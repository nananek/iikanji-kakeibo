"""外部 API (Bearer APIキー認証)"""

import functools
import hashlib
import json
import uuid
from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request, g

from app.extensions import db, limiter
from app.models.api_key import APIKey
from app.models.oauth import OAuthToken
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.ai_usage_log import AIUsageLog
from app.models.account import Account
from app.models.balance_cache import BalanceCacheBlob
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.accounting import create_journal_entry
from app.services.fiscal import check_period_open_for_new, check_entry_modifiable
from app.services.image import serve_voucher_image
from app.services.storage import (
    get_storage_backend, make_storage_key, make_thumbnail_key,
    store_image_with_thumbnail,
)
from app.services.storage_quota import (
    QuotaExceededError, check_quota, get_quota_bytes, get_used_bytes,
    maybe_send_quota_warning, record_delete, record_upload,
)
from app.services.voucher import (
    VoucherUploadConflict,
    create_voucher_from_draft,
    finalize_ai_draft_upload,
    finalize_voucher_upload,
    init_ai_draft,
    init_voucher,
)
from app.models.user import User
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.services.api_auth import auth_required, rate_limit_key
from app.views.helpers import safe_user_error

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def api_key_required(scope=None, write=False):
    """Authorization: Bearer <token> ヘッダーで認証するデコレータ

    APIキー (ik_*) と OAuth Device Flow トークン (ikt_*) の両方を受け入れる。
    OAuthToken は全スコープを暗黙的に持つ。
    scope を指定すると、API キーにそのスコープがあるか追加チェックする。
    write=True を指定したエンドポイントは、read_only な OAuth トークンからは拒否される。
    """

    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return jsonify({"error": "Authorization ヘッダーが必要です。"}), 401

            raw = auth[7:]
            now = datetime.now(timezone.utc)

            # OAuth Device Flow トークン
            if raw.startswith("ikt_"):
                token_hash = OAuthToken.hash_token(raw)
                token = OAuthToken.query.filter_by(
                    token_hash=token_hash, is_active=True
                ).first()
                if not token:
                    return jsonify({"error": "無効なトークンです。"}), 401
                if write and token.read_only:
                    return (
                        jsonify(
                            {"error": "このトークンは読み取り専用です。"}
                        ),
                        403,
                    )
                token.last_used_at = now
                db.session.commit()
                g.api_user_id = token.user_id
                return f(*args, **kwargs)

            # 従来のAPIキー
            key_hash = APIKey.hash_key(raw)
            api_key = APIKey.query.filter_by(key_hash=key_hash, is_active=True).first()
            if not api_key:
                return jsonify({"error": "無効な API キーです。"}), 401

            if scope and not api_key.has_scope(scope):
                return (
                    jsonify(
                        {"error": f"この API キーには {scope} 権限がありません。"}
                    ),
                    403,
                )

            api_key.last_used_at = now
            db.session.commit()

            g.api_user_id = api_key.user_id
            return f(*args, **kwargs)

        return decorated

    return decorator


# --- Phase E3: record-level 暗号化フィールドのデコード/エンコード ---


# blob 上限 (1 entry / line あたり)。Phase E3 設計の妥当な上限 (description 等
# 平文で 1KB 程度、ヘッダ込みで余裕を持って 4KB)。AAD ではないので大きすぎる
# blob は単にストレージ負荷になる。
_MAX_RECORD_BLOB_BYTES = 4096
_MAX_BATCH_ENTRIES = 500
# balance_cache_blobs は 1 (user, year, period) で全 P/L+B/S 科目を含むため
# record blob より大きい。標準ユーザーで数十科目 × 10〜20 byte → 1〜2KB 想定、
# 余裕を見て上限 32KB に設定 (DoS 防止)。
_MAX_BCB_BLOB_BYTES = 32 * 1024
_AES_GCM_IV_BYTES = 12

# E4 (#111) 証憑暗号文の上限。image_ct / thumb_ct は iv(12B) || ciphertext ||
# GCM tag(16B) の opaque blob。
# - 画像: 平文上限 10MB (vouchers.MAX_IMAGE_SIZE) + iv + tag + 余裕。
# - サムネ: クライアント canvas 200x200 JPEG。暗号文込みでも 512KB あれば十分。
# - meta blob: original_filename(255) + image_mime 等の小さな JSON (record 上限)。
_MAX_VOUCHER_IMAGE_CT_BYTES = 10 * 1024 * 1024 + 1024
_MAX_VOUCHER_THUMB_CT_BYTES = 512 * 1024
_GCM_MIN_BLOB_BYTES = _AES_GCM_IV_BYTES + 16  # iv + tag (平文 0B でも下回らない)


def _is_sha256_hex(s) -> bool:
    """SHA-256 の hex 文字列 (64 桁) か検証する。"""
    if not isinstance(s, str) or len(s) != 64:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def _decode_record_crypto(d: dict, label: str, blob_key: str, iv_key: str,
                          max_blob_bytes: int = None, required: bool = False):
    """payload dict から encrypted_blob / blob_iv を base64 decode して返す。

    戻り値: (blob_or_None, iv_or_None, error_message_or_None)
      - 正常で blob/iv 未指定 (required=False): (None, None, None)
      - 正常で blob/iv 指定: (bytes, bytes, None)
      - エラー: (None, None, "ユーザー向け日本語エラー")

    max_blob_bytes: blob のサイズ上限 (省略時は record (4KB) 用 default)。
      balance_cache_blobs など record より大きい blob を扱う caller が
      上書きできるようにしている。

    required=True: PR-C (E3-F dual-read 撤去) で導入。encrypted_blob/blob_iv
      の片方でも欠落していれば 400 を返す (平文-only POST 拒否)。

    例外を投げず error message を返す設計にしている理由は、呼び出し側で
    `try/except ValueError as e: return jsonify({"error": str(e)})` を行うと
    CodeQL が "Information exposure through an exception" を誤検知するため
    (自分で書いたサニタイズ済みメッセージのみを返していても、stack trace flow
    解析が flag する)。
    """
    blob_b64 = d.get(blob_key)
    iv_b64 = d.get(iv_key)
    if blob_b64 is None and iv_b64 is None:
        if required:
            return None, None, (
                f"{label}: {blob_key} と {iv_key} は必須です "
                "(クライアント側暗号化が必要)。"
            )
        return None, None, None
    if (blob_b64 is None) != (iv_b64 is None):
        return None, None, (
            f"{label}: {blob_key} と {iv_key} は同時に指定してください。"
        )
    try:
        blob = b64decode(blob_b64, validate=True)
        iv = b64decode(iv_b64, validate=True)
    except (BinasciiError, ValueError, TypeError):
        return None, None, (
            f"{label}: {blob_key}/{iv_key} の base64 が不正です。"
        )
    cap = max_blob_bytes if max_blob_bytes is not None else _MAX_RECORD_BLOB_BYTES
    if len(blob) > cap:
        return None, None, (
            f"{label}: {blob_key} が大きすぎます (max {cap}B)。"
        )
    if len(iv) != _AES_GCM_IV_BYTES:
        return None, None, (
            f"{label}: {iv_key} は {_AES_GCM_IV_BYTES}B (AES-GCM IV) である必要があります。"
        )
    return blob, iv, None


# --- 仕訳起票 ---


@bp.route("/journals", methods=["POST"])
@api_key_required(scope="journals:create", write=True)
def create_journal():
    """仕訳起票 API"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON ボディが必要です。"}), 400

    # バリデーション
    # E3-F PR-D-6-6: wire 平文除去。date / description / source は request から
    # 受け取らない (entry 本体は encrypted_blob に格納済)。平文メタは
    # fiscal_year / fiscal_month のみクライアントが算出して必須送信する。
    lines = data.get("lines")
    if not lines or not isinstance(lines, list):
        return jsonify({"error": "lines は必須です（配列）。"}), 400

    user_id = g.api_user_id

    # E3-F PR-C: クライアント側で AES-GCM 暗号化された entry 本体は必須。
    # 平文-only POST は 400 (web ブラウザは entries_builder.js 経由、外部
    # クライアントは未対応のため deprecate)。
    entry_blob, entry_iv, err = _decode_record_crypto(
        data, "entry", "encrypted_blob", "blob_iv", required=True,
    )
    if err:
        return jsonify({"error": err}), 400

    # lines_data 変換
    lines_data = []
    for i, line in enumerate(lines):
        account_code = line.get("account_code")
        if not account_code:
            return jsonify({"error": f"lines[{i}].account_code は必須です。"}), 400
        line_blob, line_iv, err = _decode_record_crypto(
            line, f"lines[{i}]", "encrypted_blob", "blob_iv",
            required=True,
        )
        if err:
            return jsonify({"error": err}), 400
        lines_data.append({
            "account_code": account_code,
            "debit_amount": int(line.get("debit", 0) or 0),
            "credit_amount": int(line.get("credit", 0) or 0),
            # E3-F PR-D-6-6: 平文 line.description は受け取らない (line 本体は
            # encrypted_blob に格納済。description 列は 055 で DROP 済)。
            "encrypted_blob": line_blob,
            "blob_iv": line_iv,
        })

    # 平文メタ (fiscal_year / fiscal_month) の検証 + 確定済み期間チェック。
    fiscal_year, fiscal_month, err = _parse_fiscal_meta(data)
    if err:
        return jsonify({"error": err}), 400
    err = check_period_open_for_new(user_id, fiscal_year, fiscal_month)
    if err:
        return jsonify({"error": err}), 400

    try:
        entry = create_journal_entry(
            user_id=user_id,
            lines_data=lines_data,
            fiscal_year=fiscal_year,
            fiscal_month=fiscal_month,
            encrypted_blob=entry_blob,
            blob_iv=entry_iv,
        )
    except ValueError as e:
        from flask import current_app
        current_app.logger.exception("create_journal_entry failed (API)")
        return jsonify({"error": safe_user_error(e)}), 400

    # draft_id が指定されていれば下書きを削除する
    draft_id = data.get("draft_id")
    if draft_id:
        try:
            draft_id = int(draft_id)
        except (TypeError, ValueError):
            return jsonify({"error": "draft_id は整数で指定してください。"}), 400
        draft = AIDraft.query.filter_by(
            id=draft_id, user_id=user_id, status="analyzed"
        ).first()
        if not draft:
            return jsonify({
                "error": f"下書き(id={draft_id})が見つからないか、既に削除済みです。"
            }), 400
        _mark_draft_done(draft, entry.entry_number)
        create_voucher_from_draft(draft, entry.id)
        db.session.commit()

    return jsonify({
        "ok": True,
        "id": entry.id,
        "entry_number": entry.entry_number,
    }), 201


def _parse_int_amount(raw, label):
    """金額フィールドを安全に int 化する。float は小数切り捨てで貸借不一致を
    隠してしまうので明示拒否する (bool は int サブクラスなので先に弾く)。
    """
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{label} は整数で指定してください (小数不可)。")
    return raw


def _validate_fiscal_meta(data, label_prefix=""):
    """E3-F PR-D-6-6: 仕訳の平文メタ (fiscal_year / fiscal_month) を検証する。

    wire 平文除去後、entry の平文メタは fiscal_year / fiscal_month のみ
    (クライアントが date から算出して必須送信する)。両者とも (値, None) を
    返し、不正時は (None, None, message) ではなく ValueError を raise する。

    fiscal_month: 0=期首 / 1-12=月 / 13-15=決算整理。16 (損益振替) は自動生成
    専用 (CLAUDE.md) なので手動 API からは拒否する。

    bool は int サブクラスなので type 直接比較で弾く。

    Returns:
        (fiscal_year, fiscal_month)
    Raises:
        ValueError: 検証失敗時
    """
    fiscal_year = data.get("fiscal_year")
    if type(fiscal_year) is not int:
        raise ValueError(f"{label_prefix}fiscal_year は整数で指定してください。")
    if not (1900 <= fiscal_year <= 2200):
        raise ValueError(
            f"{label_prefix}fiscal_year の範囲が不正です (1900〜2200)。"
        )

    fiscal_month = data.get("fiscal_month")
    if type(fiscal_month) is not int or not (0 <= fiscal_month <= 15):
        raise ValueError(
            f"{label_prefix}fiscal_month は 0〜15 の整数です "
            "(16=損益振替は自動生成専用)。"
        )
    return fiscal_year, fiscal_month


def _parse_fiscal_meta(data):
    """単一エンドポイント用に _validate_fiscal_meta を (year, month, err) で包む。"""
    try:
        fiscal_year, fiscal_month = _validate_fiscal_meta(data)
    except ValueError as e:
        return None, None, str(e)
    return fiscal_year, fiscal_month, None


def _validate_and_parse_batch_entry(e, idx, user_account_codes):
    """batch API 用: 1 entry 分の入力を validate して create_journal_entry 引数に整形。

    user_account_codes: そのユーザーの全 account.code を含む set。caller が
    バッチ処理前に 1 回だけ取得して N+1 を避ける。

    エラーは ValueError を raise (caller が一括 rollback + 400 を返す)。
    """
    if not isinstance(e, dict):
        raise ValueError(f"entries[{idx}] は dict である必要があります")

    # E3-F PR-D-6-6: wire 平文除去。date / description / source は受け取らない
    # (entry 本体は encrypted_blob に格納済)。平文メタは fiscal_year /
    # fiscal_month のみクライアントが算出して必須送信する。
    lines = e.get("lines")
    if not lines or not isinstance(lines, list):
        raise ValueError(f"entries[{idx}].lines は必須です (配列)")

    entry_blob, entry_iv, err = _decode_record_crypto(
        e, f"entries[{idx}]", "encrypted_blob", "blob_iv", required=True,
    )
    if err:
        raise ValueError(err)

    lines_data = []
    for li, line in enumerate(lines):
        account_code = line.get("account_code")
        if not account_code:
            raise ValueError(
                f"entries[{idx}].lines[{li}].account_code は必須です"
            )
        line_blob, line_iv, err = _decode_record_crypto(
            line, f"entries[{idx}].lines[{li}]",
            "encrypted_blob", "blob_iv", required=True,
        )
        if err:
            raise ValueError(err)
        # E3-F PR-D-6-6: 平文 line.description は受け取らない (line 本体は
        # encrypted_blob に格納済)。
        lines_data.append({
            "account_code": account_code,
            "debit_amount": _parse_int_amount(
                line.get("debit"), f"entries[{idx}].lines[{li}].debit",
            ),
            "credit_amount": _parse_int_amount(
                line.get("credit"), f"entries[{idx}].lines[{li}].credit",
            ),
            "encrypted_blob": line_blob,
            "blob_iv": line_iv,
        })

    # account_code がそのユーザーに存在するかチェック (FK 違反による 500 を 400 化)。
    # 全 entry の lines で使う code を caller が事前に 1 クエリで取得した
    # user_account_codes に対して set 差分で判定するため N+1 にならない。
    codes_in_entry = {ld["account_code"] for ld in lines_data}
    missing = codes_in_entry - user_account_codes
    if missing:
        raise ValueError(
            f"entries[{idx}]: 科目コード {sorted(missing)} が存在しません。"
        )

    fiscal_year, fiscal_month = _validate_fiscal_meta(
        e, label_prefix=f"entries[{idx}].",
    )

    draft_id = e.get("draft_id")
    if draft_id is not None:
        if type(draft_id) is not int or draft_id <= 0:
            raise ValueError(
                f"entries[{idx}].draft_id は正の整数で指定してください。"
            )

    return {
        "lines_data": lines_data,
        "encrypted_blob": entry_blob,
        "blob_iv": entry_iv,
        "fiscal_year": fiscal_year,
        "fiscal_month": fiscal_month,
        "draft_id": draft_id,
    }


def _batch_entries_cost():
    """レート制限: entries 件数で重み付けする (1 リクエスト最大 500 = cost 500)。

    リクエスト本体を 2 回 parse しないよう request.get_json(cache=True) に
    依存する (Flask が body bytes をキャッシュ)。parse できない場合は cost=1
    として既存の per-request 制限に任せる。
    """
    try:
        data = request.get_json(silent=True, cache=True) or {}
        entries = data.get("entries")
        if isinstance(entries, list):
            return max(1, len(entries))
    except Exception:
        pass
    return 1


@bp.route("/journals/batch", methods=["POST"])
@auth_required(write=True, scope="journals:create", allow_session=True)
@limiter.limit("30 per minute", key_func=rate_limit_key)
@limiter.limit("1500 per minute", key_func=rate_limit_key, cost=_batch_entries_cost)
def create_journals_batch():
    """複数仕訳の一括起票 API。

    web/CSV/OFX クライアント完結 E2EE 取込で使う共通エンドポイント。
    1 リクエストの全 entry を 1 トランザクションで保存する: 1 件でも
    schema validate / 確定済み期間 / 提出済みロック / 貸借不一致に
    失敗すれば全 rollback して 400 を返す。

    リクエスト:
        {
          "batch_id": "...",          // 任意、省略時は uuid4 を採番
          "entries": [{date, description, lines, ...}, ...]   // 最大 500
        }

    レスポンス:
        201 {ok, batch_id, created_count, entries: [{id, entry_number}, ...]}
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON ボディが必要です。"}), 400

    entries_in = data.get("entries")
    if not isinstance(entries_in, list) or not entries_in:
        return jsonify({"error": "entries は必須です（非空配列）。"}), 400
    if len(entries_in) > _MAX_BATCH_ENTRIES:
        return jsonify({
            "error": f"entries が多すぎます (上限 {_MAX_BATCH_ENTRIES})。",
        }), 400

    batch_id = data.get("batch_id") or str(uuid.uuid4())
    if not isinstance(batch_id, str) or len(batch_id) > 64:
        return jsonify({"error": "batch_id は文字列 (max 64) です。"}), 400

    user_id = g.auth_user.id
    # N+1 回避: ユーザーの全 account.code を一括取得して set 集合演算で
    # _validate_and_parse_batch_entry の存在チェックに使い回す。
    user_account_codes = {
        a.code for a in Account.query.filter_by(user_id=user_id).all()
    }

    created = []
    try:
        for idx, e in enumerate(entries_in):
            parsed = _validate_and_parse_batch_entry(
                e, idx, user_account_codes,
            )

            # 確定済み期間チェック (fiscal_year / fiscal_month ベース)
            err = check_period_open_for_new(
                user_id, parsed["fiscal_year"], parsed["fiscal_month"],
            )
            if err:
                raise ValueError(f"entries[{idx}]: {err}")

            entry = create_journal_entry(
                user_id=user_id,
                lines_data=parsed["lines_data"],
                batch_id=batch_id,
                encrypted_blob=parsed["encrypted_blob"],
                blob_iv=parsed["blob_iv"],
                fiscal_year=parsed["fiscal_year"],
                fiscal_month=parsed["fiscal_month"],
                commit=False,
            )

            # AI 証憑下書きの紐付け (E3-F PR-B3 quick-accept 経路)。
            # 下書きが見つからない / 既に処理済みなら全 rollback。
            if parsed["draft_id"] is not None:
                draft = AIDraft.query.filter_by(
                    id=parsed["draft_id"], user_id=user_id, status="analyzed",
                ).first()
                if draft is None:
                    raise ValueError(
                        f"entries[{idx}].draft_id: 下書きが見つからないか"
                        "既に処理済みです。"
                    )
                _mark_draft_done(draft, entry.entry_number)
                create_voucher_from_draft(draft, entry.id)

            created.append(entry)

        db.session.commit()
    except ValueError as ve:
        db.session.rollback()
        return jsonify({"error": safe_user_error(ve)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("create_journals_batch failed")
        return jsonify({"error": "一括登録に失敗しました。"}), 500

    return jsonify({
        "ok": True,
        "batch_id": batch_id,
        "created_count": len(created),
        "entries": [
            {"id": e.id, "entry_number": e.entry_number} for e in created
        ],
    }), 201


# --- 残高キャッシュ blob (E3-E-1) ---


def _validate_period(period):
    """0-16 を受け付ける (16=損益振替済の累計、確定処理後にクライアントが
    保存しうる)。範囲外なら ValueError。"""
    if type(period) is not int or not (0 <= period <= 16):
        raise ValueError(
            f"period は 0〜16 の整数で指定してください (got {period!r})"
        )


def _validate_year(year):
    if type(year) is not int or not (1900 <= year <= 2200):
        raise ValueError(
            f"year は 1900〜2200 の整数で指定してください (got {year!r})"
        )


@bp.route("/balance-cache-blobs", methods=["GET"])
@auth_required(scope="journals:read", allow_session=True)
@limiter.limit("120 per hour", key_func=rate_limit_key)
def list_balance_cache_blobs():
    """指定年の残高キャッシュ blob を一覧取得。

    Query: year=YYYY (必須)
    Response: {blobs: [{year, period, encrypted_blob, blob_iv, updated_at}]}
    クライアントが起動時に自分の MK で復号して IndexedDB に展開する。
    """
    year_str = request.args.get("year")
    if not year_str:
        return jsonify({"error": "year は必須です。"}), 400
    try:
        year = int(year_str)
        _validate_year(year)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    user_id = g.auth_user.id
    blobs = (
        BalanceCacheBlob.query
        .filter_by(user_id=user_id, year=year)
        .order_by(BalanceCacheBlob.period)
        .all()
    )
    return jsonify({
        "blobs": [
            {
                "year": b.year,
                "period": b.period,
                "encrypted_blob": b64encode(b.encrypted_blob).decode("ascii"),
                "blob_iv": b64encode(b.blob_iv).decode("ascii"),
                "updated_at": b.updated_at.isoformat() if b.updated_at else None,
            }
            for b in blobs
        ],
    })


@bp.route("/balance-cache-blobs/<int:year>/<int:period>", methods=["PUT"])
@auth_required(write=True, scope="journals:create", allow_session=True)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def upsert_balance_cache_blob(year, period):
    """(year, period) の blob を upsert する。

    Body: {encrypted_blob: base64, blob_iv: base64}
    Response: {ok, updated_at}
    """
    try:
        _validate_year(year)
        _validate_period(period)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON ボディが必要です。"}), 400

    # _decode_record_crypto は default で 4KB 上限を持つので、BCB 用の 32KB
    # 上限を明示的に渡して上書きする (default のままだと record の 4KB が
    # 効いて BCB の 32KB が dead code になる)。
    blob, iv, err = _decode_record_crypto(
        data, "blob", "encrypted_blob", "blob_iv",
        max_blob_bytes=_MAX_BCB_BLOB_BYTES,
    )
    if err:
        return jsonify({"error": err}), 400
    if blob is None:
        return jsonify({
            "error": "encrypted_blob と blob_iv は必須です。",
        }), 400

    user_id = g.auth_user.id
    existing = BalanceCacheBlob.query.filter_by(
        user_id=user_id, year=year, period=period,
    ).first()
    if existing:
        existing.encrypted_blob = blob
        existing.blob_iv = iv
        existing.updated_at = datetime.now(timezone.utc)
        target = existing
    else:
        target = BalanceCacheBlob(
            user_id=user_id, year=year, period=period,
            encrypted_blob=blob, blob_iv=iv,
        )
        db.session.add(target)
    db.session.commit()
    return jsonify({
        "ok": True,
        "updated_at": target.updated_at.isoformat() if target.updated_at else None,
    })


@bp.route("/balance-cache-blobs/<int:year>", methods=["DELETE"])
@auth_required(write=True, scope="journals:delete", allow_session=True)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def delete_balance_cache_blobs(year):
    """指定年の blob を削除。確定解除時にクライアントから呼ぶ想定。
    Query: from_period=N (任意、N 以降のみ削除。省略時は year 全部)
    scope は journals:delete (既存 delete_journal と統一)。
    """
    try:
        _validate_year(year)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    user_id = g.auth_user.id
    q = BalanceCacheBlob.query.filter_by(user_id=user_id, year=year)
    from_period_str = request.args.get("from_period")
    if from_period_str:
        try:
            from_period = int(from_period_str)
            _validate_period(from_period)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        q = q.filter(BalanceCacheBlob.period >= from_period)
    deleted = q.delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"ok": True, "deleted": deleted})


# --- 全データバックアップ (v5 BU-1) ---


@bp.route("/backup/export", methods=["GET"])
@auth_required(scope="journals:read", allow_session=True)
@limiter.limit("5 per hour", key_func=rate_limit_key)
def backup_export():
    """全データバックアップ。

    本人 (g.auth_user.id) の全テーブルのレコードを ciphertext のまま JSON で
    返す。サーバは平文を一切組立てない (E2EE 維持)。クライアントは自分の MK
    で各 encrypted_blob を復号してから平文 JSON ファイルとして保存する。

    含めるテーブル:
      accounts, fiscal_closes, journal_entries, journal_entry_lines,
      medical_expenses, balance_cache_blobs (BU-1),
      vouchers (BU-2a, active のみ。画像本体を base64 で含む),
      ai_drafts, user_ai_config (1 行), webhook_configs, tax_form_mappings,
      csv_column_profiles (BU-2b)

    含めないもの:
      webauthn_credentials, api_keys, oauth_tokens, voucher_audit_log
      → 復元時はユーザーが再登録する想定 (鍵類は災害時に再生成が安全)。

    画像取得失敗 (ストレージ欠落 / I/O エラー) はその voucher / ai_draft の
    image_data を null にして "_imageError" フィールドにメッセージを記録し、
    エクスポート全体は継続する (1 枚の欠損で全件失敗を避ける)。
    """
    from app.models.ai_config import UserAIConfig
    from app.models.csv_column_profile import CsvColumnProfile
    from app.models.fiscal import FiscalClose
    from app.models.journal import JournalEntryLine
    from app.models.medical import MedicalExpense
    from app.models.tax_form import TaxFormMapping
    from app.models.webhook import WebhookConfig

    user_id = g.auth_user.id

    accounts = Account.query.filter_by(user_id=user_id).all()
    fiscal_closes = FiscalClose.query.filter_by(user_id=user_id).all()
    entries = (
        JournalEntry.query
        .filter_by(user_id=user_id)
        .order_by(JournalEntry.entry_number)
        .all()
    )
    entry_ids = [e.id for e in entries]
    lines = (
        JournalEntryLine.query
        .filter(JournalEntryLine.journal_entry_id.in_(entry_ids))
        .all()
    ) if entry_ids else []
    medical = MedicalExpense.query.filter_by(user_id=user_id).all()
    blobs = BalanceCacheBlob.query.filter_by(user_id=user_id).all()
    vouchers = Voucher.active().filter_by(user_id=user_id).all()
    drafts = AIDraft.query.filter_by(user_id=user_id).all()
    storage = get_storage_backend() if (vouchers or drafts) else None
    ai_config = UserAIConfig.query.filter_by(user_id=user_id).first()
    webhooks = WebhookConfig.query.filter_by(user_id=user_id).all()
    tax_mappings = (
        db.session.query(TaxFormMapping)
        .filter(TaxFormMapping.user_id == user_id)
        .all()
    )
    csv_profiles = CsvColumnProfile.query.filter_by(user_id=user_id).all()

    return jsonify({
        "version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "data": {
            "accounts": [
                {
                    "code": a.code,
                    "account_type_id": a.account_type_id,
                    "name": a.name,
                    "description": a.description,
                    "tax_category": a.tax_category,
                    "cost_type": a.cost_type,
                    "system_role": a.system_role,
                    "is_system": a.is_system,
                    "is_active": a.is_active,
                    "deactivated_year": a.deactivated_year,
                    "display_order": a.display_order,
                }
                for a in accounts
            ],
            "fiscal_closes": [
                {"year": f.year, "closed_period": f.closed_period}
                for f in fiscal_closes
            ],
            # E3-F PR-D-6-4: 平文 date / description / source / fiscal_period 列は
            # export しない (クライアントが encrypted_blob を復号して平文 JSON を
            # 組み立てる。backup_export_client.js decryptBackup 参照)。fiscal_year /
            # fiscal_month / is_closing は年度フィルタ・closing 合成用の平文メタ。
            "journal_entries": [
                {
                    "id": e.id,
                    "entry_number": e.entry_number,
                    "batch_id": e.batch_id,
                    "fiscal_year": e.fiscal_year,
                    "is_closing": e.is_closing,
                    "fiscal_month": e.fiscal_month,
                    "encrypted_blob": _b64_or_none(e.encrypted_blob),
                    "blob_iv": _b64_or_none(e.blob_iv),
                }
                for e in entries
            ],
            # debit_amount / credit_amount は集計用の平文メタ列 (DROP 対象外)。
            # description 平文列は export しない (encrypted_blob に格納済)。
            "journal_entry_lines": [
                {
                    "id": l.id,
                    "journal_entry_id": l.journal_entry_id,
                    "account_code": l.account_code,
                    "debit_amount": int(l.debit_amount or 0),
                    "credit_amount": int(l.credit_amount or 0),
                    "encrypted_blob": _b64_or_none(l.encrypted_blob),
                    "blob_iv": _b64_or_none(l.blob_iv),
                }
                for l in lines
            ],
            # medical の平文列 (date / patient_name / hospital_name /
            # treatment_description / provider_type / amount_paid /
            # insurance_reimbursement) は export しない (encrypted_blob に格納済)。
            "medical_expenses": [
                {
                    "id": m.id,
                    "journal_entry_id": m.journal_entry_id,
                    "encrypted_blob": _b64_or_none(m.encrypted_blob),
                    "blob_iv": _b64_or_none(m.blob_iv),
                }
                for m in medical
            ],
            "balance_cache_blobs": [
                {
                    "year": b.year,
                    "period": b.period,
                    "encrypted_blob": _b64_or_none(b.encrypted_blob),
                    "blob_iv": _b64_or_none(b.blob_iv),
                    "updated_at": b.updated_at.isoformat() if b.updated_at else None,
                }
                for b in blobs
            ],
            "vouchers": [
                _voucher_to_backup_dict(v, storage)
                for v in vouchers
            ],
            "ai_drafts": [
                _ai_draft_to_backup_dict(d, storage)
                for d in drafts
            ],
            "user_ai_config": _ai_config_to_backup_dict(ai_config),
            "webhook_configs": [
                {
                    "id": w.id,
                    "name": w.name,
                    "provider": w.provider,
                    "webhook_url": w.webhook_url,
                    "is_active": w.is_active,
                    "events_json": w.events_json,
                    "created_at": w.created_at.isoformat() if w.created_at else None,
                    "updated_at": w.updated_at.isoformat() if w.updated_at else None,
                }
                for w in webhooks
            ],
            "tax_form_mappings": [
                {
                    "id": t.id,
                    "account_code": t.account_code,
                    "field_id": t.field_id,
                }
                for t in tax_mappings
            ],
            "csv_column_profiles": [
                {
                    "id": p.id,
                    "account_code": p.account_code,
                    "date_col": p.date_col,
                    "desc_col": p.desc_col,
                    "deposit_col": p.deposit_col,
                    "withdrawal_col": p.withdrawal_col,
                    "amount_col": p.amount_col,
                    "date_format": p.date_format,
                    "amount_mode": p.amount_mode,
                }
                for p in csv_profiles
            ],
        },
    })


def _voucher_to_backup_dict(voucher, storage):
    """Voucher 1 件を backup 用 dict に変換 (画像本体を base64 で含む)。

    画像取得失敗は image_data=None + _imageError 文字列で局所化。1 枚の I/O
    失敗で export 全体を 500 にしない。

    E4 (#111) PR-H: E2EE 証憑 (encrypted_meta_blob あり) は image_data が暗号文
    (iv||ct||tag) で、復元には暗号メタ列とクライアント生成サムネ暗号文も要る。
    - encrypted_meta_blob / meta_iv / file_hash_plain: 復号と平文側ハッシュ検証用。
    - aad_id: AAD 束縛の安定識別子。PK 再採番後も復号できるよう往復保持する。
      63bit のため JS Number 精度を超える → 文字列で受け渡す (PR-G の API と統一)。
    - thumbnail_data: クライアント暗号化サムネ (_thumb.bin) の暗号文。サーバ生成
      JPEG サムネ (平文証憑) は restore 時に Pillow で再生成するため含めない。
    """
    image_data_b64 = None
    image_error = None
    try:
        raw = storage.get(voucher.image_key)
        if raw is None:
            image_error = "storage returned None"
        else:
            image_data_b64 = b64encode(raw).decode("ascii")
    except Exception as e:
        image_error = f"{type(e).__name__}: {e}"
    thumbnail_data_b64 = None
    thumbnail_error = None
    if voucher.thumbnail_key:
        try:
            traw = storage.get(voucher.thumbnail_key)
            if traw is None:
                thumbnail_error = "storage returned None"
            else:
                thumbnail_data_b64 = b64encode(traw).decode("ascii")
        except Exception as e:
            thumbnail_error = f"{type(e).__name__}: {e}"
    out = {
        "id": voucher.id,
        "journal_entry_id": voucher.journal_entry_id,
        "image_key": voucher.image_key,
        # E5 PR-5 (#111): vouchers.image_mime 列は DROP 済。E2EE 証憑の実 MIME は
        # encrypted_meta_blob 内、平文証憑は octet-stream 配信。
        "file_hash": voucher.file_hash,
        "file_size": voucher.file_size,
        "uploaded_at": voucher.uploaded_at.isoformat() if voucher.uploaded_at else None,
        "image_data": image_data_b64,
        "encrypted_meta_blob": _b64_or_none(voucher.encrypted_meta_blob),
        "meta_iv": _b64_or_none(voucher.meta_iv),
        "file_hash_plain": voucher.file_hash_plain,
        "aad_id": str(voucher.aad_id) if voucher.aad_id is not None else None,
        "thumbnail_data": thumbnail_data_b64,
    }
    if image_error is not None:
        out["_imageError"] = image_error
    if thumbnail_error is not None:
        out["_thumbnailError"] = thumbnail_error
    return out


def _ai_draft_to_backup_dict(draft, storage):
    """AIDraft 1 件を backup 用 dict に変換 (画像本体を base64 で含む)。

    Voucher 化されていない pending/temp/analyzed/done 状態の下書きを保持する。
    画像取得は Voucher と同じ失敗局所化ポリシー。
    """
    image_data_b64 = None
    image_error = None
    try:
        raw = storage.get(draft.image_key)
        if raw is None:
            image_error = "storage returned None"
        else:
            image_data_b64 = b64encode(raw).decode("ascii")
    except Exception as e:
        image_error = f"{type(e).__name__}: {e}"
    out = {
        "id": draft.id,
        "image_key": draft.image_key,
        "image_mime": draft.image_mime,
        "file_hash": draft.file_hash,
        "file_size": draft.file_size,
        "comment": draft.comment,
        "suggestions_json": draft.suggestions_json,
        "status": draft.status,
        "discord_webhook_url": draft.discord_webhook_url,
        "discord_message_id": draft.discord_message_id,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
        "image_data": image_data_b64,
    }
    if image_error is not None:
        out["_imageError"] = image_error
    return out


def _ai_config_to_backup_dict(cfg):
    """UserAIConfig 1 行を backup 用 dict に変換。

    api_key_blob (クライアント MK で暗号化された API キー) を base64 で含める。
    サーバは復号できないので E2EE 維持。cfg=None なら null を返す。
    """
    if cfg is None:
        return None
    return {
        "id": cfg.id,
        "provider": cfg.provider,
        "model_name": cfg.model_name,
        "custom_prompt": cfg.custom_prompt,
        "compliance_check": cfg.compliance_check,
        "api_key_blob": _b64_or_none(cfg.api_key_blob),
        "api_key_iv": _b64_or_none(cfg.api_key_iv),
        "created_at": cfg.created_at.isoformat() if cfg.created_at else None,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


# --- 全データリストア (v5 BU-4b) ---


@bp.route("/backup/restore", methods=["POST"])
@auth_required(write=True, scope="backup:restore", allow_session=True)
@limiter.limit("2 per hour", key_func=rate_limit_key)
def backup_restore():
    """全置換 restore (v5 BU-4b)。

    リクエストボディに復号済み平文 backup JSON (GET /backup/export 形式) を
    POST すると、本人の全関連データを delete してから INSERT で再構築する。
    1 トランザクションでアトミック。

    監査ユーザーは破壊的操作のため禁止。`backup:restore` scope が必要。
    """
    from app.services.backup_restore import (
        BackupRestoreError,
        BackupValidationError,
        restore_user_backup,
    )

    if g.auth_user.user_type == "auditor":
        return jsonify({"error": "監査アカウントはリストア対象外です。"}), 403

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON ボディが必要です。"}), 400

    try:
        result = restore_user_backup(g.auth_user.id, payload)
    except BackupValidationError as ve:
        return jsonify({"error": safe_user_error(ve)}), 400
    except BackupRestoreError:
        return jsonify({"error": "リストアに失敗しました。"}), 500
    except Exception:
        current_app.logger.exception("backup_restore unexpected error")
        return jsonify({"error": "リストアに失敗しました。"}), 500

    return jsonify({"ok": True, "restored": result}), 200


# --- 仕訳閲覧 ---


def _b64_or_none(b):
    """LargeBinary カラム → base64 文字列 (None なら None)。"""
    return b64encode(b).decode("ascii") if b else None


def _entry_to_dict(entry):
    """JournalEntry を API レスポンス用 dict に変換。

    Phase E3: encrypted_blob / blob_iv / fiscal_year を base64 で含める。
    クライアントは blob/iv を自分の MK で復号して date / description / source /
    fiscal_period 等を取り出す。

    E3-F PR-D-6-3b: 平文 date / description / source / fiscal_period の返却を
    撤去した (これらの列は D-6-5 で DROP 予定)。期間判定・closing 判定はクラ
    イアントが保持列 fiscal_month / is_closing から行い、closing 仕訳 (暗号化
    不能で encrypted_blob 空) の date / description は fiscal_year から合成する
    (journals_client.js _normalizeEntry 参照)。
    """
    return {
        "id": entry.id,
        "entry_number": entry.entry_number,
        "fiscal_year": entry.fiscal_year,
        # E3-F: source / fiscal_period DROP 後の平文代替。is_closing は
        # 自動生成された損益振替仕訳 (encrypted_blob 空) の識別にも使う。
        "is_closing": entry.is_closing,
        "fiscal_month": entry.fiscal_month,
        "encrypted_blob": _b64_or_none(entry.encrypted_blob),
        "blob_iv": _b64_or_none(entry.blob_iv),
        "lines": [
            {
                # E3-C: line.id を返すことで AAD ("jel", user_id, entry_id,
                # line_id) の構築をクライアント側で安定させる。line index に
                # 依存しないため、将来 lines の並び替え・削除があっても
                # 既存暗号文の復号が破壊されない。
                "id": line.id,
                "account_code": line.account_code,
                "debit": int(line.debit_amount or 0),
                "credit": int(line.credit_amount or 0),
                # E3-F PR-D-6-5-pre1: 平文 description は返さない (line 本体は
                # encrypted_blob。一覧は journals_client._normalizeLine が復号
                # body.description を使う。これらの列は D-6-5 で DROP)。
                "encrypted_blob": _b64_or_none(line.encrypted_blob),
                "blob_iv": _b64_or_none(line.blob_iv),
            }
            for line in entry.lines
        ],
        "vouchers": [
            {
                "id": v.id,
                "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
            }
            for v in entry.active_vouchers
        ],
    }


@bp.route("/journals", methods=["GET"])
@auth_required(scope="journals:read")
@limiter.limit("120 per hour", key_func=rate_limit_key)
def list_journals():
    """仕訳一覧 API.

    E3-C-1b 以降、クライアント側 (ブラウザ JS の journals_client.js) からも
    fetch する。ブラウザは Cookie 認証 (Flask-Login session) で叩くため、
    `@api_key_required` (Bearer only) ではなく `@auth_required` (Bearer +
    session) を使う。`scope="journals:read"` は API キー認証時のみ要求 (OAuth
    トークン・セッション認証は scope 不問)。

    rate-limit 120/hour: ブラウザクライアントが年度別に全件取得するため、
    1 年 = 数 page (per_page=100) を想定し他 AI 系 (60/h) より緩めに設定。
    """
    user_id = g.auth_user.id
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)

    query = JournalEntry.query.filter_by(user_id=user_id)

    # E3-F PR-D-6-3: 平文 date による絞り込み (date_from / date_to) は撤去した
    # (date 列は D-6-5 で DROP 予定)。ブラウザクライアントは fiscal_year 単位で
    # 全件取得し、復号後にクライアント側で日付フィルタする。旧 Bearer 外部
    # クライアント (date_from/to 利用) は既に deprecate 済み。

    # Phase E3: fiscal_year フィルタ (date 暗号化後にレポート集計が依存)。
    # POST 時の検証と同じ範囲 1900〜2200 を要求する。
    fiscal_year_str = request.args.get("fiscal_year")
    if fiscal_year_str:
        try:
            fy = int(fiscal_year_str)
        except (ValueError, TypeError):
            return jsonify({"error": "fiscal_year は整数で指定してください。"}), 400
        if not (1900 <= fy <= 2200):
            return jsonify({
                "error": "fiscal_year の範囲が不正です (1900〜2200)。",
            }), 400
        query = query.filter(JournalEntry.fiscal_year == fy)

    total = query.count()
    # E3-F PR-D-6-3: 平文 date 順から fiscal_year / fiscal_month / entry_number
    # 順へ移行 (date 列は D-6-5 で DROP)。entry_number はユーザー単位の連番で
    # 一意なので最終 tiebreaker として順序を確定させる。
    entries = (
        query.order_by(
            JournalEntry.fiscal_year.desc(),
            JournalEntry.fiscal_month.desc(),
            JournalEntry.entry_number.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return jsonify({
        "ok": True,
        "journals": [_entry_to_dict(e) for e in entries],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@bp.route("/journals/batches", methods=["GET"])
@auth_required(scope="journals:read")
@limiter.limit("120 per hour", key_func=rate_limit_key)
def list_journal_batches():
    """インポートバッチ一覧 API (E3-F PR-D-6-3b-2).

    取込履歴ページ (journal/batches.html) のクライアント描画用。バッチ
    (batch_id) ごとに保持列メタ (件数 / 取込日時 / 削除可否) を集計し、各
    仕訳の encrypted_blob / blob_iv を返す。クライアントは blob を復号して
    種別ラベル (source) と日付範囲 (date_from / date_to) を組み立てる
    (batches_client.js)。平文 date / source は D-6-5 で DROP 予定のため
    サーバ側では読まない。closing (損益振替) 仕訳は暗号化不能で
    encrypted_blob が空のため、クライアントが is_closing / fiscal_year から
    date / source を合成する (journals_client.js _normalizeEntry と一致)。

    削除可否は delete_batch と同じ check_entry_modifiable で判定する
    (保持列 fiscal_year / fiscal_month / is_closing のみ参照、平文 date 不要)。
    ルーティング上 "/journals/<int:entry_id>" とは衝突しない ("batches" は
    int に変換できないため)。
    """
    user_id = g.auth_user.id
    entries = (
        JournalEntry.query
        .filter(
            JournalEntry.user_id == user_id,
            JournalEntry.batch_id.isnot(None),
        )
        .order_by(JournalEntry.created_at)
        .all()
    )

    groups = {}  # batch_id -> list[JournalEntry] (created_at 昇順)
    order = []   # batch_id の初出順
    for e in entries:
        if e.batch_id not in groups:
            groups[e.batch_id] = []
            order.append(e.batch_id)
        groups[e.batch_id].append(e)

    batches = []
    for batch_id in order:
        ents = groups[batch_id]
        is_closing = any(e.is_closing for e in ents)
        imported_at = min(
            (e.created_at for e in ents if e.created_at is not None),
            default=None,
        )
        if is_closing:
            deletable, delete_reason = False, "損益振替（自動生成）は削除できません"
        else:
            deletable, delete_reason = True, ""
            for e in ents:
                if check_entry_modifiable(user_id, e):
                    deletable, delete_reason = False, "確定済み期間の仕訳が含まれています"
                    break
        batches.append({
            "batch_id": batch_id,
            "count": len(ents),
            "imported_at": imported_at.isoformat() if imported_at else None,
            "is_closing": is_closing,
            "deletable": deletable,
            "delete_reason": delete_reason,
            "entries": [
                {
                    "id": e.id,
                    "fiscal_year": e.fiscal_year,
                    "is_closing": e.is_closing,
                    "encrypted_blob": _b64_or_none(e.encrypted_blob),
                    "blob_iv": _b64_or_none(e.blob_iv),
                }
                for e in ents
            ],
        })

    # 取込日時の降順 (新しい順) — 旧 get_batches の order_by(min(created_at).desc()) 相当。
    batches.sort(key=lambda b: b["imported_at"] or "", reverse=True)

    return jsonify({"ok": True, "batches": batches})


@bp.route("/journals/<int:entry_id>", methods=["GET"])
@auth_required(scope="journals:read")
@limiter.limit("120 per hour", key_func=rate_limit_key)
def get_journal(entry_id):
    """仕訳詳細 API.

    E3-C-1c: list_journals (#185) と同じく Bearer + session 両対応に統一。
    ブラウザ JS の journals_client.js が個別取得する将来用途に備える。
    rate-limit はリスト側と揃えて 120/hour。
    """
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=g.auth_user.id
    ).first()
    if not entry:
        return jsonify({"error": "仕訳が見つかりません。"}), 404

    return jsonify({"ok": True, "journal": _entry_to_dict(entry)})


# --- 仕訳削除 ---


@bp.route("/journals/<int:entry_id>", methods=["PUT"])
@auth_required(write=True, scope="journals:create", allow_session=True)
@limiter.limit("60 per minute", key_func=rate_limit_key)
def update_journal(entry_id):
    """仕訳更新 API。

    cashbook / journal の編集経路がクライアント側暗号化に移行 (PR-B1) する
    際の共通エンドポイント。1 entry 分の payload を受け取り、フィールドと
    lines を全置換する。

    リクエスト (E3-F PR-D-6-6: wire 平文除去後):
        {
          fiscal_year, fiscal_month,  // 平文メタ (必須)
          encrypted_blob, blob_iv,    // entry 本体 (必須)
          lines: [{account_code, debit, credit, encrypted_blob, blob_iv}, ...]
        }

    レスポンス:
        200 {ok, id, entry_number}

    確定済み期間 / 提出済みロック / 貸借不一致 / 科目不在 はいずれも 400。
    代理閲覧中の encrypted_blob 付き更新は 403 (AAD 不一致防止)。
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON ボディが必要です。"}), 400

    user_id = g.auth_user.id
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id,
    ).first()
    if not entry:
        return jsonify({"error": "仕訳が見つかりません。"}), 404

    # 旧 entry の編集可否 (確定済み期間)
    err = check_entry_modifiable(user_id, entry)
    if err:
        return jsonify({"error": err}), 400

    user_account_codes = {
        a.code for a in Account.query.filter_by(user_id=user_id).all()
    }

    try:
        parsed = _validate_and_parse_batch_entry(
            data, 0, user_account_codes,
        )
    except ValueError as ve:
        msg = str(ve)
        # entries[0]. プレフィックスは PUT では混乱を招くため除去する
        if msg.startswith("entries[0]"):
            msg = msg.replace("entries[0].", "").replace("entries[0]:", "").lstrip()
        return jsonify({"error": msg}), 400

    # 貸借不一致チェック (create_journal_entry に倣う)
    total_debit = sum(ld["debit_amount"] for ld in parsed["lines_data"])
    total_credit = sum(ld["credit_amount"] for ld in parsed["lines_data"])
    if total_debit != total_credit:
        return jsonify({
            "error": (
                f"貸借が一致しません（借方: {total_debit}, 貸方: {total_credit}）"
            ),
        }), 400

    # 更新後の対象期間が確定済みでないか確認 (fiscal_year / fiscal_month ベース)
    err = check_period_open_for_new(
        user_id, parsed["fiscal_year"], parsed["fiscal_month"],
    )
    if err:
        return jsonify({"error": err}), 400

    # entry フィールド更新
    # E3-F PR-D-6-6: 平文 date / description / source / fiscal_period 列は
    # DROP 済 (055)。entry の平文メタは fiscal_year / fiscal_month のみ更新する
    # (クライアント算出値)。entry 本体は encrypted_blob。
    entry.fiscal_year = parsed["fiscal_year"]
    entry.fiscal_month = parsed["fiscal_month"]
    entry.encrypted_blob = parsed["encrypted_blob"]
    entry.blob_iv = parsed["blob_iv"]

    # lines を全削除 → 新規追加
    for line in list(entry.lines):
        db.session.delete(line)
    db.session.flush()

    for ld in parsed["lines_data"]:
        line = JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=ld["account_code"],
            debit_amount=ld["debit_amount"],
            credit_amount=ld["credit_amount"],
            encrypted_blob=ld.get("encrypted_blob"),
            blob_iv=ld.get("blob_iv"),
        )
        db.session.add(line)

    db.session.commit()

    return jsonify({
        "ok": True,
        "id": entry.id,
        "entry_number": entry.entry_number,
    })


@bp.route("/journals/<int:entry_id>", methods=["DELETE"])
@api_key_required(scope="journals:delete", write=True)
def delete_journal(entry_id):
    """仕訳削除 API"""
    user_id = g.api_user_id
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id
    ).first()
    if not entry:
        return jsonify({"error": "仕訳が見つかりません。"}), 404

    # 確定済み期間チェック
    err = check_entry_modifiable(user_id, entry)
    if err:
        return jsonify({"error": err}), 400

    from app.views.journal import log_voucher_orphan
    log_voucher_orphan(entry, user_id)
    db.session.delete(entry)
    db.session.commit()

    return jsonify({"ok": True})


# --- AI 証憑仕訳 ---


_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
_ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


# 旧 POST /ai/analyze (Fernet 復号 + サーバ LLM 呼出し) は廃止。
# Bearer API クライアントは下記の 2-step フローに移行する:
#   1. POST /ai/uploads (multipart 画像 + comment) → draft_id 取得
#   2. クライアント側で MK 復号した API キーで LLM 直接呼出し →
#      PATCH /ai/drafts/<id>/suggestions で結果保存 (AIUsageLog 記録)


def _draft_to_dict(draft: AIDraft, include_suggestions: bool = False) -> dict:
    """AIDraft を API レスポンス用 dict に変換"""
    result: dict = {
        "id": draft.id,
        "status": draft.status,
        "comment": draft.comment or "",
        "created_at": draft.created_at.isoformat(),
    }
    if draft.suggestions_json:
        try:
            suggestions = json.loads(draft.suggestions_json)
            if suggestions:
                s = suggestions[0]
                result["summary"] = {
                    "title": s.get("title", ""),
                    "date": s.get("date", ""),
                    "description": s.get("entry_description", ""),
                    "amount": sum(
                        l.get("debit_amount", 0) for l in s.get("lines", [])
                    ),
                    "suggestion_count": len(suggestions),
                }
            if include_suggestions:
                result["suggestions"] = suggestions
        except (json.JSONDecodeError, IndexError):
            pass
    return result


@bp.route("/ai/drafts", methods=["GET"])
@api_key_required(scope="ai:analyze")
def ai_drafts():
    """下書き一覧 API"""
    user_id = g.api_user_id
    status = request.args.get("status", "analyzed")
    if status not in ("analyzed", "done", "all"):
        return jsonify({"error": "status は analyzed / done / all のいずれかです。"}), 400

    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 100)

    query = AIDraft.query.filter_by(user_id=user_id)
    if status != "all":
        query = query.filter_by(status=status)
    else:
        query = query.filter(AIDraft.status.in_(["analyzed", "done"]))

    total = query.count()
    drafts = (
        query.order_by(AIDraft.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return jsonify({
        "ok": True,
        "drafts": [_draft_to_dict(d) for d in drafts],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@bp.route("/ai/drafts/<int:draft_id>", methods=["GET"])
@api_key_required(scope="ai:analyze")
def ai_draft_detail(draft_id):
    """下書き詳細 API（候補含む）"""
    draft = AIDraft.query.filter_by(
        id=draft_id, user_id=g.api_user_id
    ).first()
    if not draft or draft.status == "temp":
        return jsonify({"error": "下書きが見つかりません。"}), 404

    return jsonify({
        "ok": True,
        "draft": _draft_to_dict(draft, include_suggestions=True),
    })


@bp.route("/ai/drafts/<int:draft_id>", methods=["DELETE"])
@api_key_required(scope="ai:analyze", write=True)
def ai_draft_delete(draft_id):
    """下書き削除 API"""
    draft = AIDraft.query.filter_by(
        id=draft_id, user_id=g.api_user_id
    ).first()
    if not draft or draft.status == "temp":
        return jsonify({"error": "下書きが見つかりません。"}), 404

    image_key = draft.image_key
    # Phase 5 #70: Voucher 化されないことが確定するため StorageUsage 減算。
    owner_id = draft.user_id
    size_to_release = draft.file_size or 0
    db.session.delete(draft)
    db.session.commit()
    storage = get_storage_backend()
    storage.delete(image_key)
    storage.delete(make_thumbnail_key(image_key))
    if size_to_release > 0:
        owner = db.session.get(User, owner_id)
        if owner is not None:
            record_delete(owner, size_to_release)

    return jsonify({"ok": True})


@bp.route("/ai/prompt-context", methods=["GET"])
@auth_required(write=False)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def ai_prompt_context():
    """クライアント側で Round 1+2 プロンプトを組み立てるための
    材料をサーバから一括取得する endpoint。

    クライアントは:
      1. 画像 + round1_prompt + (compliance/custom_prompt 追記) で LLM 呼出
      2. Round 1 結果から needs_ledger=true なら ledger 取得 endpoint (別 PR)
      3. needs_ledger に応じてテンプレートを選択し、プレースホルダ置換:
         - needs_ledger=false → round2_prompt_template_no_ledger を使用、
           __ACCOUNT_LIST_TEXT__ のみ置換 (元帳ヘッダは含まれない)
         - needs_ledger=true  → round2_prompt_template_with_ledger を使用、
           __ACCOUNT_LIST_TEXT__ と __LEDGER_TEXT__ を置換
         で Round 2 プロンプト構築 → 2 度目の LLM 呼出
      4. PATCH /api/v1/ai/drafts/<id>/suggestions で結果保存

    本 endpoint はサーバ側 ai_receipt.py の DOCUMENT_PROMPT / COMPLIANCE_CHECK_PROMPT /
    _get_account_list_text / _build_suggestion_prompt と等価のメタデータを返却。
    Round 2 は no_ledger / with_ledger の 2 種類のテンプレートを返却して
    needs_ledger=false 時に「以下は関連する元帳...」ヘッダのみ残るバグを防ぐ
    (custom_prompt はサーバで埋込済)。
    """
    user_id = g.auth_user.id
    from app.services.ai_receipt import (
        COMPLIANCE_CHECK_PROMPT,
        DOCUMENT_PROMPT,
        PROVIDER_DEFAULTS,
        _build_suggestion_prompt,
        _get_account_list_text,
    )

    # AI 設定 (provider 別デフォルトモデル + custom_prompt + compliance_check)
    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    custom_prompt = config.custom_prompt if config else ""
    compliance_check = bool(config and config.compliance_check)

    # 勘定科目一覧 (サーバで既に計算しているもの)
    account_list_text = _get_account_list_text(user_id)

    # Round 2 プロンプトテンプレートを 2 種類生成する。
    # _build_suggestion_prompt は ledger_text が空かどうかで「以下は関連する
    # 勘定科目の元帳データ...」ヘッダの有無を切り替えるため、ヘッダのみ
    # 残って中身が空になるバグ (PR #154 review 3) を防ぐには、needs_ledger
    # で 2 テンプレートを切り替える方が安全。
    # クライアントは needs_ledger に応じてどちらか 1 つを選び、
    # __ACCOUNT_LIST_TEXT__ を置換する (with_ledger 版はさらに __LEDGER_TEXT__
    # も置換)。custom_prompt はサーバで埋め込み済 (再置換不要)。
    round2_template_no_ledger = _build_suggestion_prompt(
        account_list_text="__ACCOUNT_LIST_TEXT__",
        ledger_text="",
        custom_prompt=custom_prompt,
    )
    round2_template_with_ledger = _build_suggestion_prompt(
        account_list_text="__ACCOUNT_LIST_TEXT__",
        ledger_text="__LEDGER_TEXT__",
        custom_prompt=custom_prompt,
    )

    return jsonify({
        "ok": True,
        # Round 1 プロンプト (compliance/custom_prompt はクライアント側で append)
        "round1_prompt": DOCUMENT_PROMPT,
        "compliance_prompt": COMPLIANCE_CHECK_PROMPT,
        "compliance_check_enabled": compliance_check,
        # Round 2 プロンプトテンプレート 2 種類。クライアントは Round 1 結果の
        # needs_ledger に応じてどちらか選び、__ACCOUNT_LIST_TEXT__ を置換する
        # (with_ledger 版はさらに __LEDGER_TEXT__ も置換):
        #   needs_ledger=false → round2_prompt_template_no_ledger
        #                        (元帳ヘッダなし)
        #   needs_ledger=true  → round2_prompt_template_with_ledger
        #                        (__LEDGER_TEXT__ を実 ledger で置換)
        # custom_prompt はサーバ側で既に埋め込み済み (再置換不要)。
        "round2_prompt_template_no_ledger": round2_template_no_ledger,
        "round2_prompt_template_with_ledger": round2_template_with_ledger,
        # __ACCOUNT_LIST_TEXT__ プレースホルダ置換用に別途返却。
        # クライアント側 account_code バリデーション (実在チェック) にも使用。
        "account_list_text": account_list_text,
        # クライアント Round 1 にユーザー custom_prompt を append する場合に使う
        "custom_prompt": custom_prompt,
        # デフォルトモデル (UserAIConfig.model_name 未指定時)。
        # サーバ側 ai_receipt.PROVIDER_DEFAULTS と一致させ、E2EE クライアントと
        # 既存サーバ解析 (/ai/analyze) が同じモデルを使うことを保証する。
        "default_model_by_provider": dict(PROVIDER_DEFAULTS),
    })


@bp.route("/suggest-categories/prompt-context", methods=["GET"])
@auth_required(write=False)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def suggest_categories_prompt_context():
    """E2EE suggest-categories: クライアント側 LLM 呼出しのためのプロンプト材料を返す。

    payment_account_code クエリパラメータ必須 (口座名・account_map のため)。
    レスポンスには勘定科目コード → 名前マップ (account_map) も含めて、
    クライアントが LLM 出力の account_code から account_name を解決できる
    ようにする。

    E3-F PR-D-6-1a: 元帳テキスト (ledger_context) はサーバ側で平文
    JournalEntry.date / description を読んで構築していたが撤去した。
    クライアント (suggest_categories_orchestrator.js) が復号済み仕訳から
    buildPaymentLedgerContext で組み立てる。
    """
    from app.models.account import Account
    from app.services.ai_receipt import (
        AI_SUGGEST_CATEGORIES_PROMPT_TEMPLATE,
        PROVIDER_DEFAULTS,
        _get_account_list_text,
    )

    user_id = g.auth_user.id
    payment_account_code = request.args.get("payment_account_code", "").strip()
    if not payment_account_code:
        return jsonify({"error": "payment_account_code が必要です。"}), 400

    account = Account.query.filter_by(
        user_id=user_id, code=payment_account_code,
    ).first()
    if not account:
        return jsonify({"error": "指定された口座が存在しません。"}), 400

    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    custom_prompt = config.custom_prompt if config else ""

    account_list = _get_account_list_text(user_id)

    accounts = Account.query.filter_by(
        user_id=user_id, is_active=True,
    ).all()
    account_map = {a.code: a.name for a in accounts}

    return jsonify({
        "ok": True,
        "prompt_template": AI_SUGGEST_CATEGORIES_PROMPT_TEMPLATE,
        "payment_account_name": account.name,
        "account_list": account_list,
        "account_map": account_map,
        "custom_prompt": custom_prompt,
        "default_model_by_provider": dict(PROVIDER_DEFAULTS),
    })


@bp.route("/voucher-attach/prompt-context", methods=["GET"])
@auth_required(write=False)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def voucher_attach_prompt_context():
    """E2EE voucher-attach: クライアント側 LLM 呼出しのためのプロンプト材料を返す。"""
    user_id = g.auth_user.id
    from app.services.ai_receipt import (
        COMPLIANCE_CHECK_PROMPT,
        CONSISTENCY_CHECK_PROMPT_TEMPLATE,
        DOCUMENT_PROMPT,
        PROVIDER_DEFAULTS,
    )

    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    compliance_check = bool(config and config.compliance_check)

    # サーバ側 analyze_voucher_for_attachment と等価のプロンプト構築:
    # DOCUMENT_PROMPT + (compliance なら COMPLIANCE_CHECK_PROMPT) +
    # CONSISTENCY_CHECK_PROMPT_TEMPLATE (placeholder のまま、クライアント側で
    # __JOURNAL_DATE__ / __JOURNAL_AMOUNT__ / __JOURNAL_DESCRIPTION__ を置換)
    prompt = DOCUMENT_PROMPT
    if compliance_check:
        prompt += COMPLIANCE_CHECK_PROMPT
    prompt += CONSISTENCY_CHECK_PROMPT_TEMPLATE

    return jsonify({
        "ok": True,
        "prompt_template": prompt,
        "compliance_check_enabled": compliance_check,
        "default_model_by_provider": dict(PROVIDER_DEFAULTS),
    })


@bp.route("/web-import/prompt-context", methods=["GET"])
@auth_required(write=False)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def web_import_prompt_context():
    """E2EE web-import: クライアント側 LLM 呼出しのためのプロンプト材料を返す。"""
    user_id = g.auth_user.id
    from app.services.ai_receipt import PROVIDER_DEFAULTS, WEB_IMPORT_PROMPT

    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    custom_prompt = config.custom_prompt if config else ""

    return jsonify({
        "ok": True,
        # __PAYMENT_ACCOUNT_NAME__ と __RAW_TEXT__ をクライアントで置換する
        "prompt_template": WEB_IMPORT_PROMPT,
        "custom_prompt": custom_prompt,
        "default_model_by_provider": dict(PROVIDER_DEFAULTS),
    })


# 旧 POST /api/v1/ai/ledger-context (AI証憑 Round2 の元帳テキスト返却) は
# E3-F PR-D-6-1b で削除。平文 JournalEntry.date / description を読んでいたため、
# クライアント (ai_journal_orchestrator.js → crypto/ledger_context.js
# buildAccountsLedgerContext) が復号済み仕訳から構築する。


# クライアント側 LLM 呼出フロー用 endpoint。
# サーバ側で LLM を呼ばないため /ai/analyze と異なり API キーが不要。
# クライアントが画像をアップロード → 自分で LLM を呼ぶ → 結果を PATCH で保存。

# AIDraft.suggestions_json のサイズ上限 (DB レコード巨大化防止)。
# json.dumps(..., ensure_ascii=False) で日本語は UTF-8 で 1 文字 = 3 バイトに
# なるため、文字数でなく **UTF-8 バイト数** で判定する。
_MAX_SUGGESTIONS_JSON_SIZE = 200 * 1024  # 200 KB (UTF-8 bytes)


@bp.route("/ai/uploads", methods=["POST"])
@auth_required(write=True)
@limiter.limit("30 per hour", key_func=rate_limit_key)
def ai_upload():
    """E2EE 移行用: 画像のみアップロードし、AIDraft を pending 状態で作成する。

    クライアントは戻り値の draft_id を使って自分で LLM を呼び、
    PATCH /api/v1/ai/drafts/<id>/suggestions で結果を保存する。

    multipart/form-data:
      image: 画像ファイル (必須)
      comment: メモ (任意、最大 500 文字)
    """
    user_id = g.auth_user.id

    image_file = request.files.get("image")
    if not image_file or not image_file.filename:
        return jsonify({"error": "image is required"}), 400

    image_bytes = image_file.read()
    if len(image_bytes) > _MAX_IMAGE_SIZE:
        return jsonify({"error": "ファイルサイズが大きすぎます (上限 10MB)"}), 400

    mime_type = image_file.content_type
    if mime_type not in _ALLOWED_MIME_TYPES:
        return jsonify({
            "error": "対応していないファイル形式です (JPEG/PNG/WebP/GIF)",
        }), 400

    size = len(image_bytes)
    owner = db.session.get(User, user_id)
    if owner is None:
        return jsonify({"error": "user not found"}), 400
    try:
        check_quota(owner, size)
    except QuotaExceededError as exc:
        return jsonify({"error": exc.user_message}), 413

    file_hash = hashlib.sha256(image_bytes).hexdigest()
    comment = (request.form.get("comment") or "").strip()[:500]

    # status="pending": クライアントが LLM 呼出を完了するまでの中間状態
    # (suggestions_json は空配列で初期化)
    draft = AIDraft(
        user_id=user_id,
        image_key="",
        image_mime=mime_type,
        file_hash=file_hash,
        file_size=size,
        comment=comment or None,
        suggestions_json="[]",
        status="pending",
    )
    db.session.add(draft)
    db.session.flush()
    key = make_storage_key(draft.user_id, draft.id, mime_type)
    store_image_with_thumbnail(key, image_bytes, mime_type)
    draft.image_key = key
    db.session.commit()

    # quota 加算 + TOCTOU 楽観検証 (/ai/analyze と同じパターン)
    record_upload_succeeded = False
    try:
        record_upload(owner, size)
        record_upload_succeeded = True
    except Exception as e:
        from flask import current_app
        current_app.logger.exception(
            "api ai/uploads: record_upload failed (user=%d size=%d): %s",
            owner.id, size, e,
        )
    if record_upload_succeeded and get_used_bytes(owner) > get_quota_bytes(owner):
        from flask import current_app
        storage = get_storage_backend()
        for k in (key, make_thumbnail_key(key)):
            try:
                storage.delete(k)
            except Exception as e:
                current_app.logger.warning(
                    "api ai/uploads rollback: storage delete failed %s: %s", k, e,
                )
        db.session.delete(draft)
        db.session.commit()
        try:
            record_delete(owner, size)
        except Exception as e:
            current_app.logger.exception(
                "api ai/uploads rollback: record_delete failed: %s", e,
            )
        return jsonify({
            "error": "並行アップロードにより容量上限を超えました。",
        }), 413

    maybe_send_quota_warning(owner)

    return jsonify({
        "ok": True,
        "draft_id": draft.id,
        "status": draft.status,
    }), 201


@bp.route("/ai/drafts/<int:draft_id>/suggestions", methods=["PATCH"])
@auth_required(write=True)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def ai_draft_save_suggestions(draft_id):
    """E2EE 移行用: クライアント側 LLM の解析結果を AIDraft に保存。

    Body (JSON):
      suggestions: list — クライアント LLM が返した仕訳候補 (AI 形式)
      usage: dict (任意) — input_tokens / output_tokens
    """
    user_id = g.auth_user.id
    draft = AIDraft.query.filter_by(id=draft_id, user_id=user_id).first()
    if not draft:
        return jsonify({"error": "下書きが見つかりません"}), 404
    # pending → analyzed / 既に analyzed の場合の再上書きも許容
    # (LLM 再実行 / suggestions 編集ケース)
    if draft.status not in ("pending", "analyzed", "temp"):
        return jsonify({"error": "current status cannot accept suggestions"}), 400

    payload = request.get_json(silent=True) or {}
    suggestions = payload.get("suggestions")
    if not isinstance(suggestions, list):
        return jsonify({"error": "suggestions must be a list"}), 400
    suggestions_json = json.dumps(suggestions, ensure_ascii=False)
    if len(suggestions_json.encode("utf-8")) > _MAX_SUGGESTIONS_JSON_SIZE:
        return jsonify({
            "error": f"suggestions too large (max {_MAX_SUGGESTIONS_JSON_SIZE} bytes)",
        }), 413

    draft.suggestions_json = suggestions_json
    draft.status = "analyzed"

    # クライアント LLM の利用量をサーバ側でも記録する。サーバ側 ai_receipt.py
    # フローと等価の監査トレイル + Phase 3 Billing 連携に必要。
    # provider / model / usage が揃っているリクエストのみ記録 (任意フィールド)。
    # 負値は誤送信・不正クライアント対策で弾く (Billing 連携の前提)。
    provider = payload.get("provider")
    model = payload.get("model")
    usage = payload.get("usage") or {}
    raw_input = usage.get("input_tokens") if isinstance(usage, dict) else None
    raw_output = usage.get("output_tokens") if isinstance(usage, dict) else None

    def _non_negative_int(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    input_tokens = _non_negative_int(raw_input)
    output_tokens = _non_negative_int(raw_output)
    if (
        isinstance(provider, str) and provider
        and isinstance(model, str) and model
    ):
        total = None
        if input_tokens is not None and output_tokens is not None:
            total = input_tokens + output_tokens
        log = AIUsageLog(
            user_id=user_id,
            provider=provider[:20],
            model=model[:100],
            feature="receipt_client_side",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            status="ok",
        )
        db.session.add(log)

    db.session.commit()

    return jsonify({
        "ok": True,
        "draft": _draft_to_dict(draft, include_suggestions=True),
    })


# --- E5 (#111): AI 下書き画像の 2 段階 E2EE upload ---


@bp.route("/ai/uploads/init", methods=["POST"])
@auth_required(write=True)
@limiter.limit("10 per minute", key_func=rate_limit_key)
def init_ai_draft_endpoint():
    """E5 (#111) 2 段階 upload Step 1: AIDraft を採番する (空 row 作成)。

    クライアントは init レスポンスの aad_id を AAD (`vimg`/`vthumb`/`vmeta` +
    user_id + aad_id、voucher と同ドメイン) に束縛して画像/サムネ/メタを暗号化し、
    Step 2 (`PUT /ai/uploads/<id>`) で実体を upload する。draft_id は LLM 解析
    結果の保存先 (`PATCH /ai/drafts/<id>/suggestions`) としても使う。

    リクエスト: {"comment": "<str|null>"}
    レスポンス: 201 {"ok": true, "draft_id": <int>, "aad_id": "<str>"}

    aad_id は 63bit のため、JS Number の 2^53 精度を超えて欠落しないよう
    **文字列**で返す (クライアントは BigInt でパースして AAD に束縛する)。

    下書きは E2EE のみで、監査人は owner の MK を持たないため owner が復号可能な
    暗号文を作れない (voucher init と同じ)。
    """
    user_id = g.auth_user.id
    data = request.get_json(silent=True) or {}
    comment = (data.get("comment") or "").strip()[:500]

    draft = init_ai_draft(user_id, comment or None)
    db.session.commit()
    return jsonify({
        "ok": True,
        "draft_id": draft.id,
        "aad_id": str(draft.aad_id),
    }), 201


@bp.route("/ai/uploads/<int:draft_id>", methods=["PUT"])
@auth_required(write=True)
@limiter.limit("30 per minute", key_func=rate_limit_key)
def upload_ai_draft_endpoint(draft_id):
    """E5 (#111) 2 段階 upload Step 2: 暗号文の実体を upload する (multipart)。

    フォーム (voucher PUT と同形式):
      - image_ct (file): iv(12B) || ciphertext || GCM tag の opaque blob。必須。
      - thumb_ct (file): サムネイル暗号文 (同形式)。任意。
      - meta_blob (str): encrypted_meta_blob の base64。必須。
      - meta_iv (str): meta blob の 12B IV の base64。必須。
      - file_hash_plain (str): SHA-256(平文画像) の hex 64 桁。必須。

    サーバは file_hash_cipher = SHA-256(image_ct) を計算して保存する。
    init 直後の空 row のみ受け付け、既にアップロード済みなら 409。
    """
    user_id = g.auth_user.id
    draft = AIDraft.query.filter_by(id=draft_id, user_id=user_id).first()
    if not draft:
        return jsonify({"error": "下書きが見つかりません。"}), 404

    # 上書き禁止: init 直後の空 row のみ受け付ける。
    if draft.image_key or draft.encrypted_meta_blob is not None:
        return jsonify({
            "error": "この下書きは既にアップロード済みです。上書きはできません。",
        }), 409

    image_part = request.files.get("image_ct")
    if image_part is None:
        return jsonify({"error": "image_ct (暗号文画像) は必須です。"}), 400
    image_ct = image_part.read()
    if not (_GCM_MIN_BLOB_BYTES <= len(image_ct) <= _MAX_VOUCHER_IMAGE_CT_BYTES):
        return jsonify({
            "error": "image_ct のサイズが不正です (iv+tag 未満、または上限超過)。",
        }), 400

    thumb_ct = None
    thumb_part = request.files.get("thumb_ct")
    if thumb_part is not None:
        thumb_ct = thumb_part.read()
        if not (_GCM_MIN_BLOB_BYTES <= len(thumb_ct) <= _MAX_VOUCHER_THUMB_CT_BYTES):
            return jsonify({
                "error": "thumb_ct のサイズが不正です。",
            }), 400

    meta_blob, meta_iv, err = _decode_record_crypto(
        request.form, "meta", "meta_blob", "meta_iv", required=True,
    )
    if err:
        return jsonify({"error": err}), 400

    file_hash_plain = request.form.get("file_hash_plain")
    if not _is_sha256_hex(file_hash_plain):
        return jsonify({
            "error": "file_hash_plain は SHA-256 の hex 64 桁である必要があります。",
        }), 400

    try:
        finalize_ai_draft_upload(
            draft, image_ct, thumb_ct, meta_blob, meta_iv, file_hash_plain,
        )
    except VoucherUploadConflict:
        return jsonify({
            "error": "この下書きは既にアップロード済みです。上書きはできません。",
        }), 409
    except QuotaExceededError as exc:
        return jsonify({"error": exc.user_message}), 413

    return jsonify({
        "ok": True,
        "draft_id": draft.id,
        "status": draft.status,
        "file_hash_cipher": draft.file_hash,
    }), 200


# --- E4 (#111): 証憑画像の 2 段階 E2EE upload ---


@bp.route("/vouchers/init", methods=["POST"])
@auth_required(write=True, scope="journals:create", allow_session=True)
@limiter.limit("10 per minute", key_func=rate_limit_key)
def init_voucher_endpoint():
    """2 段階 upload Step 1: voucher_id を採番する (空 row 作成)。

    クライアントは init レスポンスの aad_id を AAD (`vimg`/`vthumb`/`vmeta` +
    user_id + aad_id) に束縛して画像/サムネ/メタを暗号化し、Step 2
    (`PUT /vouchers/<id>`) で実体を upload する。aad_id は voucher_id と独立した
    安定識別子で、backup/restore の PK 再採番後もクライアント復号を可能にする
    (E4 #111 Option C)。

    リクエスト: {"journal_entry_id": <int|null>}
    レスポンス: 201 {"ok": true, "voucher_id": <int>, "aad_id": "<str>"}

    aad_id は 63bit のため、JS Number の 2^53 精度を超えて欠落しないよう
    **文字列**で返す (クライアントは BigInt でパースして AAD に束縛する)。

    証憑は E2EE のみで平文 write 経路が無く、監査人は owner の MK を持たない
    ため owner が復号可能な暗号文を作れない。
    """
    user_id = g.auth_user.id
    data = request.get_json(silent=True) or {}

    journal_entry_id = data.get("journal_entry_id")
    if journal_entry_id is not None:
        try:
            journal_entry_id = int(journal_entry_id)
        except (TypeError, ValueError):
            return jsonify({"error": "journal_entry_id は整数で指定してください。"}), 400
        entry = JournalEntry.query.filter_by(
            id=journal_entry_id, user_id=user_id,
        ).first()
        if not entry:
            return jsonify({"error": "仕訳が見つかりません。"}), 404

    voucher = init_voucher(user_id, journal_entry_id)
    db.session.commit()
    return jsonify({
        "ok": True,
        "voucher_id": voucher.id,
        "aad_id": str(voucher.aad_id),
    }), 201


@bp.route("/vouchers/<int:voucher_id>", methods=["PUT"])
@auth_required(write=True, scope="journals:create", allow_session=True)
@limiter.limit("30 per minute", key_func=rate_limit_key)
def upload_voucher_endpoint(voucher_id):
    """2 段階 upload Step 2: 暗号文の実体を upload する (multipart/form-data)。

    フォーム:
      - image_ct (file): iv(12B) || ciphertext || GCM tag の opaque blob。必須。
      - thumb_ct (file): サムネイル暗号文 (同形式)。任意。
      - meta_blob (str): encrypted_meta_blob の base64。必須。
      - meta_iv (str): meta blob の 12B IV の base64。必須。
      - file_hash_plain (str): SHA-256(平文画像) の hex 64 桁。必須。

    サーバは file_hash_cipher = SHA-256(image_ct) を計算して保存する。
    電帳法の改ざん防止のため、既に確定済みの証憑への上書きは 409 で拒否する。
    """
    user_id = g.auth_user.id
    voucher = Voucher.active().filter_by(
        id=voucher_id, user_id=user_id,
    ).first()
    if not voucher:
        return jsonify({"error": "証憑が見つかりません。"}), 404

    # 上書き禁止 (電帳法): init 直後の空 row のみ受け付ける。
    if voucher.image_key or voucher.encrypted_meta_blob is not None:
        return jsonify({
            "error": "この証憑は既にアップロード済みです。上書きはできません。",
        }), 409

    image_part = request.files.get("image_ct")
    if image_part is None:
        return jsonify({"error": "image_ct (暗号文画像) は必須です。"}), 400
    image_ct = image_part.read()
    if not (_GCM_MIN_BLOB_BYTES <= len(image_ct) <= _MAX_VOUCHER_IMAGE_CT_BYTES):
        return jsonify({
            "error": "image_ct のサイズが不正です (iv+tag 未満、または上限超過)。",
        }), 400

    thumb_ct = None
    thumb_part = request.files.get("thumb_ct")
    if thumb_part is not None:
        thumb_ct = thumb_part.read()
        if not (_GCM_MIN_BLOB_BYTES <= len(thumb_ct) <= _MAX_VOUCHER_THUMB_CT_BYTES):
            return jsonify({
                "error": "thumb_ct のサイズが不正です。",
            }), 400

    # meta blob + iv (DB 格納のため _decode_record_crypto で base64 decode + 検証)
    meta_blob, meta_iv, err = _decode_record_crypto(
        request.form, "meta", "meta_blob", "meta_iv", required=True,
    )
    if err:
        return jsonify({"error": err}), 400

    file_hash_plain = request.form.get("file_hash_plain")
    if not _is_sha256_hex(file_hash_plain):
        return jsonify({
            "error": "file_hash_plain は SHA-256 の hex 64 桁である必要があります。",
        }), 400

    try:
        finalize_voucher_upload(
            voucher, image_ct, thumb_ct, meta_blob, meta_iv, file_hash_plain,
        )
    except VoucherUploadConflict:
        # 並行 PUT が原子的クレームで弾かれた (PR-B レビュー ①)。CodeQL の
        # py/stack-trace-exposure 誤検知を避けるため固定文言を返す。
        return jsonify({
            "error": "この証憑は既にアップロード済みです。上書きはできません。",
        }), 409
    except QuotaExceededError as exc:
        return jsonify({"error": exc.user_message}), 413

    return jsonify({
        "ok": True,
        "voucher_id": voucher.id,
        "file_hash_cipher": voucher.file_hash,
    }), 200


@bp.route("/vouchers", methods=["GET"])
@api_key_required(scope="journals:read")
def list_vouchers():
    """証憑一覧 API"""
    from sqlalchemy import func
    from app.models.journal import JournalEntryLine

    user_id = g.api_user_id
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)

    query = (
        Voucher.active()
        .outerjoin(JournalEntry, Voucher.journal_entry_id == JournalEntry.id)
        .filter(Voucher.user_id == user_id)
    )

    # E3-F PR-D-6-3: 平文 date / description による絞り込み (date_from /
    # date_to / search) は撤去した (両列は D-6-5 で DROP)。電帳法の検索要件は
    # ブラウザの証憑一覧 (クライアント側で復号データを検索) が満たす。本 Bearer
    # API は外部クライアント専用で既に deprecate 済み。

    amount_from = request.args.get("amount_from", type=int)
    amount_to = request.args.get("amount_to", type=int)
    if amount_from is not None or amount_to is not None:
        amount_subq = (
            db.session.query(
                JournalEntryLine.journal_entry_id,
                func.sum(JournalEntryLine.debit_amount).label("total"),
            )
            .group_by(JournalEntryLine.journal_entry_id)
            .subquery()
        )
        query = query.outerjoin(
            amount_subq, JournalEntry.id == amount_subq.c.journal_entry_id
        )
        if amount_from is not None:
            query = query.filter(amount_subq.c.total >= amount_from)
        if amount_to is not None:
            query = query.filter(amount_subq.c.total <= amount_to)

    total = query.count()
    # E3-F PR-D-6-3: 平文 date によるソートを撤去し uploaded_at (証憑の保存
    # 時刻、DROP 対象外) 降順に変更した。
    vouchers = (
        query.order_by(
            Voucher.uploaded_at.desc(),
            Voucher.id.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    result = []
    for v in vouchers:
        entry = v.journal_entry
        d = {
            "id": v.id,
            "journal_entry_id": v.journal_entry_id,
            # E5 PR-5 (#111): image_mime 列は DROP 済 (上記参照)。
            # E4 PR-G (Option C): 画像/サムネ復号の AAD 束縛用安定識別子。
            # 63bit のため文字列 (JS Number 精度対策)。平文証憑は null。
            "aad_id": str(v.aad_id) if v.aad_id is not None else None,
            "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
        }
        if entry:
            # E3-F PR-D-6-3: 平文 date / description の返却を撤去した
            # (D-6-5 で DROP)。amount は line.debit_amount 由来で平文保持。
            # 入力期限警告 (deadline_exceeded) は date 依存のため撤去 (電帳法
            # の期限チェックはブラウザ証憑一覧が担う)。
            d["journal"] = {
                "amount": int(entry.total_debit),
            }
        else:
            d["journal"] = None
        result.append(d)

    return jsonify({
        "ok": True,
        "vouchers": result,
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@bp.route("/vouchers/<int:voucher_id>/image", methods=["GET"])
@api_key_required(scope="journals:read")
def api_voucher_image(voucher_id):
    """証憑画像取得 API"""
    voucher = Voucher.active().filter_by(
        id=voucher_id, user_id=g.api_user_id
    ).first()
    if not voucher:
        return jsonify({"error": "証憑が見つかりません。"}), 404
    try:
        return serve_voucher_image(voucher)
    except FileNotFoundError:
        return jsonify({"error": "画像ファイルが見つかりません。"}), 404


@bp.route("/vouchers/<int:voucher_id>/verify", methods=["GET"])
@api_key_required(scope="journals:read")
def api_voucher_verify(voucher_id):
    """証憑ハッシュ検証 API"""
    voucher = Voucher.active().filter_by(
        id=voucher_id, user_id=g.api_user_id
    ).first()
    if not voucher:
        return jsonify({"error": "証憑が見つかりません。"}), 404

    if not voucher.file_hash:
        return jsonify({"ok": True, "verified": None, "message": "ハッシュ未記録"})

    try:
        image_data = get_storage_backend().get(voucher.image_key)
    except FileNotFoundError:
        return jsonify({"error": "画像ファイルが見つかりません。"}), 404

    computed = hashlib.sha256(image_data).hexdigest()
    verified = computed == voucher.file_hash

    db.session.add(VoucherAuditLog(
        voucher_id=voucher.id,
        user_id=g.api_user_id,
        action="hash_verified" if verified else "hash_mismatch",
    ))
    db.session.commit()

    return jsonify({
        "ok": True,
        "verified": verified,
        "stored_hash": voucher.file_hash,
        "computed_hash": computed,
    })


@bp.route("/vouchers/<int:voucher_id>/logs", methods=["GET"])
@api_key_required(scope="journals:read")
def api_voucher_logs(voucher_id):
    """証憑操作ログ API。

    削除済 (`deleted_at` セット済) の Voucher にも引き続きアクセス可能。
    電帳法の訂正削除証跡として、削除後にログを参照したい運用がある。
    """
    voucher = Voucher.query.filter_by(
        id=voucher_id, user_id=g.api_user_id
    ).first()
    if not voucher:
        return jsonify({"error": "証憑が見つかりません。"}), 404

    logs = (
        VoucherAuditLog.query
        .filter_by(voucher_id=voucher.id)
        .order_by(VoucherAuditLog.created_at.desc())
        .all()
    )

    # E4 PR-D: 平文 detail は返さない (server 生成ログは detail を持たず、
    # 証跡は action + created_at + 持続する voucher 行で担保)。代わりに
    # encrypted_detail_blob / detail_iv (base64、valog AAD) を返す。これらは
    # クライアントが供給した暗号化ノートがある場合のみ非 null で、クライアント
    # が MK で復号する。平文 detail 列は PR-F で DROP 予定。
    return jsonify({
        "ok": True,
        "logs": [
            {
                "id": log.id,
                "action": log.action,
                "encrypted_detail_blob": (
                    b64encode(log.encrypted_detail_blob).decode()
                    if log.encrypted_detail_blob else None
                ),
                "detail_iv": (
                    b64encode(log.detail_iv).decode()
                    if log.detail_iv else None
                ),
                "created_at": log.created_at.isoformat(),
                "user_id": log.user_id,
            }
            for log in logs
        ],
    })


# --- レポート API (Phase E3-F-4a で撤去) ---
#
# `/api/v1/reports/trial-balance`, `/income-statement`, `/monthly`, `/tax`
# はサーバ側集計に依存していたが、E2EE 化により仕訳が暗号化されると
# サーバ側で復号できないため意味を成さなくなる。client-py からは未使用
# (調査済) のため撤去。client は `/api/v1/journals` (Bearer/session 両対応)
# で entries を取得し、各 compute_*_view.js (試算表/P/L/B/S/月次比較/
# 元帳/医療費/確定申告控除/決算書) で集計する設計。


# --- 医療費 API (Phase E3-C-8b) ---


@bp.route("/medical-expenses", methods=["GET"])
@auth_required(scope="journals:read")
@limiter.limit("120 per hour", key_func=rate_limit_key)
def list_medical_expenses():
    """医療費 (MedicalExpense) 一覧 API。

    Phase E3-C-8b: クライアント側 medical_expenses_client が
    fetchMedicalExpensesForYear で取得する。年度 (?fiscal_year=) で
    フィルタ。本テーブルは通常 1 年あたり数百件以下なのでページネは省略。

    レスポンス:
    - 平文フィールド (dual-read 期間中の互換、Phase E7 で削除予定)
    - encrypted_blob / blob_iv (base64、クライアント側で MK 復号)
    """
    from app.models.medical import MedicalExpense
    from app.models.journal import JournalEntry as _JE
    user_id = g.auth_user.id

    query = MedicalExpense.query.filter_by(user_id=user_id)

    fy_str = request.args.get("fiscal_year")
    if fy_str:
        try:
            fy = int(fy_str)
        except (ValueError, TypeError):
            return jsonify({"error": "fiscal_year は整数で指定してください。"}), 400
        if not (1900 <= fy <= 2200):
            return jsonify({
                "error": "fiscal_year の範囲が不正です (1900〜2200)。",
            }), 400
        # 紐付く JournalEntry の fiscal_year (E3-A で導入、date 暗号化後も有効)
        query = (
            query.join(_JE, MedicalExpense.journal_entry_id == _JE.id)
            .filter(_JE.fiscal_year == fy)
        )

    expenses = query.order_by(MedicalExpense.id).all()
    return jsonify({
        "ok": True,
        "expenses": [
            {
                "id": e.id,
                "journal_entry_id": e.journal_entry_id,
                # E3-F PR-D-6-5-pre1: 平文 (date/patient_name/hospital_name/
                # treatment_description/provider_type/amount_paid/
                # insurance_reimbursement) は返さない。client は
                # medical_expenses_client._normalizeExpense で encrypted_blob を
                # 復号して取り出す (これらの列は D-6-5 で DROP)。
                "encrypted_blob": _b64_or_none(e.encrypted_blob),
                "blob_iv": _b64_or_none(e.blob_iv),
            }
            for e in expenses
        ],
        "total": len(expenses),
    })


@bp.route("/medical-expenses", methods=["POST"])
@auth_required(write=True, scope="journals:create", allow_session=True)
@limiter.limit("60 per minute", key_func=rate_limit_key)
def upsert_medical_expense():
    """医療費明細 (MedicalExpense) の作成 or 更新 API (Phase E3-F PR-D-3)。

    journal_entry_id で upsert する (1 仕訳 1 医療費明細という前提。既存の
    medical.api_update と同じ create-or-update セマンティクス)。医療費 UI が
    サーバレンダ + 平文 POST から client 描画 + client 暗号化に移行する経路。

    リクエスト (E3-F PR-D-6-6: wire 平文除去後):
        {
          journal_entry_id,
          encrypted_blob, blob_iv,            // 必須 (PR-C 同様、平文-only 拒否)
          // 平文 date / patient_name / hospital_name / treatment_description /
          // provider_type / amount_paid / insurance_reimbursement は受け取らない
          // (本体は encrypted_blob のみ。列は 055 で DROP 済)。
        }
    レスポンス: 200 {ok, id}

    仕訳不在/他人 → 404、確定済み期間/提出済みロック → 400、
    代理閲覧中の暗号化書込み → 403。
    """
    from app.models.medical import MedicalExpense

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON ボディが必要です。"}), 400

    user_id = g.auth_user.id

    journal_entry_id = data.get("journal_entry_id")
    try:
        journal_entry_id = int(journal_entry_id)
    except (TypeError, ValueError):
        return jsonify({"error": "journal_entry_id は整数で指定してください。"}), 400

    entry = JournalEntry.query.filter_by(
        id=journal_entry_id, user_id=user_id,
    ).first()
    if not entry:
        return jsonify({"error": "仕訳が見つかりません。"}), 404

    # 確定済み期間の伝票は医療費明細も変更不可。
    err = check_entry_modifiable(user_id, entry)
    if err:
        return jsonify({"error": err}), 400

    # E3-F PR-C: クライアント側で AES-GCM 暗号化された本体は必須。
    blob, iv, err = _decode_record_crypto(
        data, "medical", "encrypted_blob", "blob_iv", required=True,
    )
    if err:
        return jsonify({"error": err}), 400

    # E3-F PR-D-6-6: 平文列 (date / patient_name / hospital_name /
    # treatment_description / provider_type / amount_paid /
    # insurance_reimbursement) は wire からも撤去済。本体は encrypted_blob のみ
    # に格納する (列は 055 で DROP 済)。
    me = MedicalExpense.query.filter_by(
        journal_entry_id=journal_entry_id, user_id=user_id,
    ).first()
    if me is None:
        me = MedicalExpense(user_id=user_id, journal_entry_id=journal_entry_id)
        db.session.add(me)

    me.encrypted_blob = blob
    me.blob_iv = iv
    db.session.commit()

    return jsonify({"ok": True, "id": me.id})


def _mark_draft_done(draft: AIDraft, entry_number: int):
    """仕訳登録後に Discord 通知を完了マークに更新する"""
    if not draft.discord_message_id or not draft.discord_webhook_url:
        return
    from app.services.notify import update_discord_message

    original_desc = ""
    if draft.suggestions_json:
        try:
            suggestions = json.loads(draft.suggestions_json)
            if suggestions:
                s = suggestions[0]
                parts = []
                if s.get("date"):
                    parts.append(s["date"])
                if s.get("entry_description"):
                    parts.append(s["entry_description"])
                original_desc = " ".join(parts)
        except (json.JSONDecodeError, IndexError):
            pass

    message = ""
    if original_desc:
        message += f"~~{original_desc}~~\n"
    message += f"仕訳を登録しました（伝票 #{entry_number}）"

    update_discord_message(
        webhook_url=draft.discord_webhook_url,
        message_id=draft.discord_message_id,
        title="✅ いいかんじ™家計簿 AI仕訳",
        message=message,
    )
