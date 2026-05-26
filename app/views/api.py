"""外部 API (Bearer APIキー認証)"""

import functools
import hashlib
import json
import uuid
from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from datetime import date as date_type, datetime, timezone

from flask import Blueprint, current_app, jsonify, request, g

from app.extensions import db, limiter
from app.models.api_key import APIKey
from app.models.oauth import OAuthToken
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.ai_usage_log import AIUsageLog
from app.models.account import Account
from app.models.balance_cache import BalanceCacheBlob
from app.models.journal import JournalEntry
from app.services.accounting import create_journal_entry
from app.services.audit import get_submitted_account_codes, is_entry_locked_for_owner
from app.services.fiscal import check_period_open_for_new, check_entry_modifiable
from app.services.image import serve_image
from app.services.storage import (
    get_storage_backend, make_storage_key, make_thumbnail_key,
    store_image_with_thumbnail,
)
from app.services.storage_quota import (
    QuotaExceededError, check_quota, get_quota_bytes, get_used_bytes,
    maybe_send_quota_warning, record_delete, record_upload,
)
from app.services.voucher import create_voucher_from_draft
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
_ALLOWED_BATCH_SOURCES = {
    "journal", "cashbook", "ai_receipt", "csv", "ofx", "web", "api",
}
_AES_GCM_IV_BYTES = 12


def _decode_record_crypto(d: dict, label: str, blob_key: str, iv_key: str,
                          max_blob_bytes: int = None):
    """payload dict から encrypted_blob / blob_iv を base64 decode して返す。

    戻り値: (blob_or_None, iv_or_None, error_message_or_None)
      - 正常で blob/iv 未指定: (None, None, None)
      - 正常で blob/iv 指定: (bytes, bytes, None)
      - エラー: (None, None, "ユーザー向け日本語エラー")

    max_blob_bytes: blob のサイズ上限 (省略時は record (4KB) 用 default)。
      balance_cache_blobs など record より大きい blob を扱う caller が
      上書きできるようにしている。

    例外を投げず error message を返す設計にしている理由は、呼び出し側で
    `try/except ValueError as e: return jsonify({"error": str(e)})` を行うと
    CodeQL が "Information exposure through an exception" を誤検知するため
    (自分で書いたサニタイズ済みメッセージのみを返していても、stack trace flow
    解析が flag する)。
    """
    blob_b64 = d.get(blob_key)
    iv_b64 = d.get(iv_key)
    if blob_b64 is None and iv_b64 is None:
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
    date_str = data.get("date")
    description = (data.get("description") or "").strip()
    lines = data.get("lines")
    source = data.get("source", "api")

    if not date_str:
        return jsonify({"error": "date は必須です。"}), 400
    if not description:
        return jsonify({"error": "description は必須です。"}), 400
    if not lines or not isinstance(lines, list):
        return jsonify({"error": "lines は必須です（配列）。"}), 400

    try:
        entry_date = date_type.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "date の形式が不正です（YYYY-MM-DD）。"}), 400

    user_id = g.api_user_id

    # 確定済み期間チェック
    err = check_period_open_for_new(user_id, entry_date.year, entry_date.month)
    if err:
        return jsonify({"error": err}), 400

    # Phase E3: クライアント側で AES-GCM 暗号化された entry 本体 (任意)。
    # 両方セットされていなければ無視 (= 旧 dual storage の平文保存のみ)。
    entry_blob, entry_iv, err = _decode_record_crypto(
        data, "entry", "encrypted_blob", "blob_iv",
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
        )
        if err:
            return jsonify({"error": err}), 400
        lines_data.append({
            "account_code": account_code,
            "debit_amount": int(line.get("debit", 0) or 0),
            "credit_amount": int(line.get("credit", 0) or 0),
            "description": line.get("description", ""),
            "encrypted_blob": line_blob,
            "blob_iv": line_iv,
        })

    # 提出済みロック科目チェック
    locked_codes = get_submitted_account_codes(user_id)
    if locked_codes:
        used_codes = {ld["account_code"] for ld in lines_data}
        if used_codes & locked_codes:
            return jsonify({"error": "提出済みの税務科目を含むため登録できません。"}), 400

    # Phase E3: fiscal_year は date 暗号化後の年度フィルタ用の平文カラム。
    # クライアントが明示指定しなければ date.year を採用 (service 側のデフォルト)。
    fiscal_year = data.get("fiscal_year")
    if fiscal_year is not None:
        # bool は int サブクラスなので isinstance では弾けない。type 直接比較。
        if type(fiscal_year) is not int:
            return jsonify({"error": "fiscal_year は整数で指定してください。"}), 400
        if not (1900 <= fiscal_year <= 2200):
            return jsonify({
                "error": "fiscal_year の範囲が不正です (1900〜2200)。",
            }), 400

    try:
        entry = create_journal_entry(
            user_id=user_id,
            date=entry_date,
            description=description,
            lines_data=lines_data,
            source=source,
            encrypted_blob=entry_blob,
            blob_iv=entry_iv,
            fiscal_year=fiscal_year,
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


def _validate_and_parse_batch_entry(e, idx, user_account_codes, locked_codes):
    """batch API 用: 1 entry 分の入力を validate して create_journal_entry 引数に整形。

    user_account_codes: そのユーザーの全 account.code を含む set。caller が
    バッチ処理前に 1 回だけ取得して N+1 を避ける。

    エラーは ValueError を raise (caller が一括 rollback + 400 を返す)。
    """
    if not isinstance(e, dict):
        raise ValueError(f"entries[{idx}] は dict である必要があります")

    date_str = e.get("date")
    if not date_str:
        raise ValueError(f"entries[{idx}].date は必須です")
    try:
        entry_date = date_type.fromisoformat(date_str)
    except (TypeError, ValueError):
        raise ValueError(f"entries[{idx}].date の形式が不正です (YYYY-MM-DD)")

    description = (e.get("description") or "").strip()
    if not description:
        raise ValueError(f"entries[{idx}].description は必須です")
    # JournalEntry.description は String(255)。超過すると DB エラー (500) になる
    # ので API 層で 400 として返す。
    if len(description) > 255:
        raise ValueError(
            f"entries[{idx}].description は 255 文字以内で指定してください"
        )

    lines = e.get("lines")
    if not lines or not isinstance(lines, list):
        raise ValueError(f"entries[{idx}].lines は必須です (配列)")

    entry_blob, entry_iv, err = _decode_record_crypto(
        e, f"entries[{idx}]", "encrypted_blob", "blob_iv",
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
            "encrypted_blob", "blob_iv",
        )
        if err:
            raise ValueError(err)
        line_desc = line.get("description", "") or ""
        if len(line_desc) > 255:
            raise ValueError(
                f"entries[{idx}].lines[{li}].description は 255 文字以内で指定してください"
            )
        lines_data.append({
            "account_code": account_code,
            "debit_amount": _parse_int_amount(
                line.get("debit"), f"entries[{idx}].lines[{li}].debit",
            ),
            "credit_amount": _parse_int_amount(
                line.get("credit"), f"entries[{idx}].lines[{li}].credit",
            ),
            "description": line_desc,
            "encrypted_blob": line_blob,
            "blob_iv": line_iv,
        })

    if locked_codes:
        used_codes = {ld["account_code"] for ld in lines_data}
        if used_codes & locked_codes:
            raise ValueError(
                f"entries[{idx}]: 提出済みの税務科目を含むため登録できません。"
            )

    # account_code がそのユーザーに存在するかチェック (FK 違反による 500 を 400 化)。
    # 全 entry の lines で使う code を caller が事前に 1 クエリで取得した
    # user_account_codes に対して set 差分で判定するため N+1 にならない。
    codes_in_entry = {ld["account_code"] for ld in lines_data}
    missing = codes_in_entry - user_account_codes
    if missing:
        raise ValueError(
            f"entries[{idx}]: 科目コード {sorted(missing)} が存在しません。"
        )

    fiscal_year = e.get("fiscal_year")
    if fiscal_year is not None:
        if type(fiscal_year) is not int:
            raise ValueError(
                f"entries[{idx}].fiscal_year は整数で指定してください。"
            )
        if not (1900 <= fiscal_year <= 2200):
            raise ValueError(
                f"entries[{idx}].fiscal_year の範囲が不正です (1900〜2200)。"
            )

    fiscal_period = e.get("fiscal_period")
    if fiscal_period is not None:
        # fiscal_period=16 (損益振替) は自動生成専用 (CLAUDE.md 参照)。
        # 手動 API から指定できないように 0〜15 に制限する。
        if type(fiscal_period) is not int or not (0 <= fiscal_period <= 15):
            raise ValueError(
                f"entries[{idx}].fiscal_period は 0〜15 の整数です "
                "(16=損益振替は自動生成専用)。"
            )

    source = e.get("source", "api")
    if source not in _ALLOWED_BATCH_SOURCES:
        raise ValueError(
            f"entries[{idx}].source の値が不正です: {source!r}"
        )

    return {
        "date": entry_date,
        "description": description,
        "lines_data": lines_data,
        "source": source,
        "encrypted_blob": entry_blob,
        "blob_iv": entry_iv,
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
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
    locked_codes = get_submitted_account_codes(user_id)
    # N+1 回避: ユーザーの全 account.code を一括取得して set 集合演算で
    # _validate_and_parse_batch_entry の存在チェックに使い回す。
    user_account_codes = {
        a.code for a in Account.query.filter_by(user_id=user_id).all()
    }

    created = []
    try:
        for idx, e in enumerate(entries_in):
            parsed = _validate_and_parse_batch_entry(
                e, idx, user_account_codes, locked_codes,
            )

            # 確定済み期間チェック (validate と分離: date が parsed 後でないと判定不可)
            err = check_period_open_for_new(
                user_id, parsed["date"].year, parsed["date"].month,
            )
            if err:
                raise ValueError(f"entries[{idx}]: {err}")

            entry = create_journal_entry(
                user_id=user_id,
                date=parsed["date"],
                description=parsed["description"],
                lines_data=parsed["lines_data"],
                source=parsed["source"],
                batch_id=batch_id,
                fiscal_period=parsed["fiscal_period"],
                encrypted_blob=parsed["encrypted_blob"],
                blob_iv=parsed["blob_iv"],
                fiscal_year=parsed["fiscal_year"],
                commit=False,
            )
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

    TODO(E3-F): 監査代理閲覧 (session["acting_as_user_id"]) に未対応。
    resolve_bearer_or_session が acting_as_user_id を見るようになったら、
    Lv3 監査員のセッションでも対象ユーザーの blob が返るようになる。
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
# TODO(E3-F): 監査代理閲覧 (acting_as_user_id) 対応は resolve_bearer_or_session
# 側の改修待ち。代理閲覧中に PUT すると現状は監査員自身の blob を更新する。
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
# TODO(E3-F): 監査代理閲覧 (acting_as_user_id) 対応は resolve_bearer_or_session
# 側の改修待ち。
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

    含めるテーブル (Phase BU-1):
      accounts, fiscal_closes, journal_entries, journal_entry_lines,
      medical_expenses, balance_cache_blobs

    含めないもの (今 PR では):
      vouchers (画像本体), ai_drafts (画像本体), user_ai_configs (API キー),
      webhook_configs, tax_form_mappings, csv_column_profiles, webauthn_*
      → 次の BU-PR で順次対応。

    監査代理閲覧では他人のデータを export できない: 監査者の MK ではオーナーの
    encrypted_blob を復号できないため、結果として復号失敗するが、API としては
    監査者自身のデータが返るだけ (acting_as 解決は将来 PR)。
    """
    from app.models.fiscal import FiscalClose
    from app.models.journal import JournalEntryLine
    from app.models.medical import MedicalExpense

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
            "journal_entries": [
                {
                    "id": e.id,
                    "date": e.date.isoformat() if e.date else None,
                    "entry_number": e.entry_number,
                    "description": e.description,
                    "source": e.source,
                    "batch_id": e.batch_id,
                    "fiscal_period": e.fiscal_period,
                    "fiscal_year": e.fiscal_year,
                    "encrypted_blob": _b64_or_none(e.encrypted_blob),
                    "blob_iv": _b64_or_none(e.blob_iv),
                }
                for e in entries
            ],
            "journal_entry_lines": [
                {
                    "id": l.id,
                    "journal_entry_id": l.journal_entry_id,
                    "account_code": l.account_code,
                    "debit_amount": int(l.debit_amount or 0),
                    "credit_amount": int(l.credit_amount or 0),
                    "description": l.description,
                    "encrypted_blob": _b64_or_none(l.encrypted_blob),
                    "blob_iv": _b64_or_none(l.blob_iv),
                }
                for l in lines
            ],
            "medical_expenses": [
                {
                    "id": m.id,
                    "journal_entry_id": m.journal_entry_id,
                    "date": m.date.isoformat() if m.date else None,
                    "patient_name": m.patient_name,
                    "hospital_name": m.hospital_name,
                    "treatment_description": m.treatment_description,
                    "provider_type": m.provider_type,
                    "amount_paid": int(m.amount_paid or 0),
                    "insurance_reimbursement": int(m.insurance_reimbursement or 0),
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
        },
    })


# --- 仕訳閲覧 ---


def _b64_or_none(b):
    """LargeBinary カラム → base64 文字列 (None なら None)。"""
    return b64encode(b).decode("ascii") if b else None


def _entry_to_dict(entry):
    """JournalEntry を API レスポンス用 dict に変換。

    Phase E3: encrypted_blob / blob_iv / fiscal_year を base64 で含める。
    クライアントは blob/iv がセットされていれば自分の MK で復号、なければ
    旧平文フィールド (date / description / lines[].account_code 等) を使う。
    """
    return {
        "id": entry.id,
        "date": entry.date.isoformat(),
        "entry_number": entry.entry_number,
        "description": entry.description,
        "source": entry.source,
        # E3-C: クライアント側 dual-read 時に fiscal_period が必要 (期首仕訳
        # 等の月次集計、設計書 §12.7)。
        "fiscal_period": entry.fiscal_period,
        "fiscal_year": entry.fiscal_year,
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
                "description": line.description or "",
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

    date_from = request.args.get("date_from")
    if date_from:
        try:
            query = query.filter(JournalEntry.date >= date_type.fromisoformat(date_from))
        except ValueError:
            return jsonify({"error": "date_from の形式が不正です（YYYY-MM-DD）。"}), 400

    date_to = request.args.get("date_to")
    if date_to:
        try:
            query = query.filter(JournalEntry.date <= date_type.fromisoformat(date_to))
        except ValueError:
            return jsonify({"error": "date_to の形式が不正です（YYYY-MM-DD）。"}), 400

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
    entries = (
        query.order_by(JournalEntry.date.desc(), JournalEntry.entry_number.desc())
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

    # 提出済みロック科目チェック
    if is_entry_locked_for_owner(user_id, entry):
        return jsonify({"error": "提出済みの税務科目を含む伝票のため削除できません。"}), 400

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
        # llama_cpp はサーバ管理者提供前提 + v5.0 で廃止のため除外。
        "default_model_by_provider": {
            k: v for k, v in PROVIDER_DEFAULTS.items() if k != "llama_cpp"
        },
    })


@bp.route("/suggest-categories/prompt-context", methods=["GET"])
@auth_required(write=False)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def suggest_categories_prompt_context():
    """E2EE suggest-categories: クライアント側 LLM 呼出しのためのプロンプト材料を返す。

    payment_account_code クエリパラメータ必須 (元帳テキストを構築するため)。
    レスポンスには勘定科目コード → 名前マップ (account_map) も含めて、
    クライアントが LLM 出力の account_code から account_name を解決できる
    ようにする。
    """
    from app.models.account import Account
    from app.services.ai_receipt import (
        AI_SUGGEST_CATEGORIES_PROMPT_TEMPLATE,
        PROVIDER_DEFAULTS,
        _get_account_list_text,
        _get_payment_ledger_context,
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

    ledger_context = _get_payment_ledger_context(user_id, payment_account_code)
    account_list = _get_account_list_text(user_id)

    accounts = Account.query.filter_by(
        user_id=user_id, is_active=True,
    ).all()
    account_map = {a.code: a.name for a in accounts}

    return jsonify({
        "ok": True,
        "prompt_template": AI_SUGGEST_CATEGORIES_PROMPT_TEMPLATE,
        "payment_account_name": account.name,
        "ledger_context": ledger_context,
        "account_list": account_list,
        "account_map": account_map,
        "custom_prompt": custom_prompt,
        "default_model_by_provider": {
            k: v for k, v in PROVIDER_DEFAULTS.items() if k != "llama_cpp"
        },
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
        "default_model_by_provider": {
            k: v for k, v in PROVIDER_DEFAULTS.items() if k != "llama_cpp"
        },
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
        # llama_cpp はサーバ管理者提供前提 + v5.0 で廃止予定のため除外
        "default_model_by_provider": {
            k: v for k, v in PROVIDER_DEFAULTS.items() if k != "llama_cpp"
        },
    })


@bp.route("/ai/ledger-context", methods=["POST"])
@auth_required(write=False)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def ai_ledger_context():
    """Round 2 LLM 入力用に、requested_accounts の元帳テキストを返却。

    クライアントは Round 1 結果 (needs_ledger=true) の requested_accounts を
    送信し、本 endpoint が _get_ledger_context() でテキスト整形して返す。

    Body (JSON):
      account_names: list[str] — 元帳取得対象の科目名 (Round 1 LLM の出力由来)

    Returns:
      { ledger_text: "整形済テキスト" }
      該当科目なしや空入力なら ledger_text="".

    注意: 本 endpoint は v5.0 (E2EE プレビュー段階) では仕訳データが平文の
    まま返却される。E3 (仕訳暗号化) 以降は、クライアントが復号済仕訳から
    自分でテキスト構築する設計に変更が必要 (本 endpoint は廃止予定)。
    """
    user_id = g.auth_user.id
    # `_get_ledger_context` は ai_receipt.py の内部関数 (`_` プレフィックス)
    # だが、本 endpoint は移行期間の互換層であり、ai_receipt.py 全体削除時に
    # 本 endpoint も同時に削除予定 (E3 では仕訳暗号化でクライアント側 ledger
    # 構築に変わる)。短命なので意図的に内部関数を直接流用している。
    from app.services.ai_receipt import _get_ledger_context

    payload = request.get_json(silent=True) or {}
    account_names = payload.get("account_names")
    if not isinstance(account_names, list):
        return jsonify({"error": "account_names must be a list"}), 400
    # 文字列のみ受理、最大 20 個 (LLM がリクエストする科目数は通常 1-5 個)
    filtered = [
        n for n in account_names
        if isinstance(n, str) and 0 < len(n) <= 100
    ][:20]
    if not filtered:
        return jsonify({"ledger_text": ""})

    ledger_text = _get_ledger_context(user_id, filtered)
    return jsonify({"ledger_text": ledger_text})


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

    date_from = request.args.get("date_from")
    if date_from:
        try:
            d = date_type.fromisoformat(date_from)
            query = query.filter(
                db.or_(
                    JournalEntry.date >= d,
                    db.and_(Voucher.journal_entry_id.is_(None),
                            func.date(Voucher.uploaded_at) >= date_from),
                )
            )
        except ValueError:
            return jsonify({"error": "date_from の形式が不正です。"}), 400

    date_to = request.args.get("date_to")
    if date_to:
        try:
            d = date_type.fromisoformat(date_to)
            query = query.filter(
                db.or_(
                    JournalEntry.date <= d,
                    db.and_(Voucher.journal_entry_id.is_(None),
                            func.date(Voucher.uploaded_at) <= date_to),
                )
            )
        except ValueError:
            return jsonify({"error": "date_to の形式が不正です。"}), 400

    search = request.args.get("search")
    if search:
        query = query.filter(JournalEntry.description.ilike(f"%{search}%"))

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
    vouchers = (
        query.order_by(
            func.coalesce(JournalEntry.date, func.date(Voucher.uploaded_at)).desc(),
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
            "image_mime": v.image_mime,
            "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
        }
        if entry:
            d["journal"] = {
                "date": entry.date.isoformat(),
                "description": entry.description,
                "amount": int(entry.total_debit),
            }
            if v.uploaded_at:
                d["deadline_exceeded"] = (v.uploaded_at.date() - entry.date).days > 67
            else:
                d["deadline_exceeded"] = False
        else:
            d["journal"] = None
            d["deadline_exceeded"] = False
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
        return serve_image(voucher.image_key, voucher.image_mime, voucher.file_hash)
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

    return jsonify({
        "ok": True,
        "logs": [
            {
                "id": log.id,
                "action": log.action,
                "detail": log.detail,
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
                # 平文 (dual-read 互換)。Phase E7 で削除予定。
                "date": e.date.isoformat() if e.date else None,
                "patient_name": e.patient_name,
                "hospital_name": e.hospital_name,
                "treatment_description": e.treatment_description,
                "provider_type": e.provider_type,
                "amount_paid": int(e.amount_paid or 0),
                "insurance_reimbursement": int(e.insurance_reimbursement or 0),
                # Phase E3: クライアント MK で AES-GCM 暗号化済の本体
                "encrypted_blob": _b64_or_none(e.encrypted_blob),
                "blob_iv": _b64_or_none(e.blob_iv),
            }
            for e in expenses
        ],
        "total": len(expenses),
    })


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
