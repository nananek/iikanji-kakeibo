"""外部 API (Bearer APIキー認証)"""

import functools
import hashlib
import json
from dataclasses import asdict
from datetime import date as date_type, datetime, timezone

from flask import Blueprint, jsonify, request, g

from app.extensions import db, limiter
from app.models.api_key import APIKey
from app.models.oauth import OAuthToken
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.ai_usage_log import AIUsageLog
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

    # lines_data 変換
    lines_data = []
    for i, line in enumerate(lines):
        account_code = line.get("account_code")
        if not account_code:
            return jsonify({"error": f"lines[{i}].account_code は必須です。"}), 400
        lines_data.append({
            "account_code": account_code,
            "debit_amount": int(line.get("debit", 0) or 0),
            "credit_amount": int(line.get("credit", 0) or 0),
            "description": line.get("description", ""),
        })

    # 提出済みロック科目チェック
    locked_codes = get_submitted_account_codes(user_id)
    if locked_codes:
        used_codes = {ld["account_code"] for ld in lines_data}
        if used_codes & locked_codes:
            return jsonify({"error": "提出済みの税務科目を含むため登録できません。"}), 400

    try:
        entry = create_journal_entry(
            user_id=user_id,
            date=entry_date,
            description=description,
            lines_data=lines_data,
            source=source,
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


# --- 仕訳閲覧 ---


def _entry_to_dict(entry):
    """JournalEntry を API レスポンス用 dict に変換"""
    return {
        "id": entry.id,
        "date": entry.date.isoformat(),
        "entry_number": entry.entry_number,
        "description": entry.description,
        "source": entry.source,
        "lines": [
            {
                "account_code": line.account_code,
                "debit": int(line.debit_amount or 0),
                "credit": int(line.credit_amount or 0),
                "description": line.description or "",
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
@api_key_required(scope="journals:read")
def list_journals():
    """仕訳一覧 API"""
    user_id = g.api_user_id
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
@api_key_required(scope="journals:read")
def get_journal(entry_id):
    """仕訳詳細 API"""
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=g.api_user_id
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


@bp.route("/ai/analyze", methods=["POST"])
@limiter.limit("30/hour")
@api_key_required(scope="ai:analyze", write=True)
def ai_analyze():
    """画像を AI 解析して下書きを作成する。

    multipart/form-data:
      image: 画像ファイル (必須)
      comment: メモ (任意, 最大500文字)
      notify: "1" でWebhook通知を送信 (任意)
    """
    from app.services.ai_receipt import analyze_and_suggest

    user_id = g.api_user_id

    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    if not config:
        return jsonify({"error": "AI API設定が未登録です。"}), 400

    image_file = request.files.get("image")
    if not image_file or not image_file.filename:
        return jsonify({"error": "image は必須です。"}), 400

    image_bytes = image_file.read()
    if len(image_bytes) > _MAX_IMAGE_SIZE:
        return jsonify({"error": "ファイルサイズが大きすぎます（上限10MB）。"}), 400

    mime_type = image_file.content_type
    if mime_type not in _ALLOWED_MIME_TYPES:
        return jsonify({
            "error": "対応していないファイル形式です。JPEG/PNG/WebP/GIF を使用してください。"
        }), 400

    # Phase 5 #70: AIDraft 段階で StorageUsage に計上 (ai_journal.analyze と同等)
    size = len(image_bytes)
    owner = db.session.get(User, user_id)
    if owner is None:
        return jsonify({"error": "ユーザーが見つかりません。"}), 400
    try:
        check_quota(owner, size)
    except QuotaExceededError as exc:
        # CodeQL py/stack-trace-exposure 対策で `exc.user_message` 経由 (PR #92)
        return jsonify({"error": exc.user_message}), 413

    file_hash = hashlib.sha256(image_bytes).hexdigest()
    comment = (request.form.get("comment") or "").strip()[:500]

    try:
        suggestions = analyze_and_suggest(
            user_id, image_bytes, mime_type, comment=comment or None,
        )
    except (ValueError, RuntimeError) as e:
        from flask import current_app
        current_app.logger.exception("analyze_and_suggest failed (API)")
        return jsonify({"error": safe_user_error(e)}), 400

    suggestions_data = [asdict(s) for s in suggestions]
    suggestions_json = json.dumps(suggestions_data, ensure_ascii=False)

    draft = AIDraft(
        user_id=user_id,
        image_key="",
        image_mime=mime_type,
        file_hash=file_hash,
        file_size=size,
        comment=comment or None,
        suggestions_json=suggestions_json,
        status="analyzed",
    )
    db.session.add(draft)
    db.session.flush()
    key = make_storage_key(draft.user_id, draft.id, mime_type)
    store_image_with_thumbnail(key, image_bytes, mime_type)
    draft.image_key = key
    db.session.commit()

    # 容量加算 + TOCTOU 楽観的再検証 (create_voucher_from_upload と同じパターン)。
    # Draft は既に commit 済のため、record_upload の例外で 500 を返すと
    # quota リークになる。明示的に握ってログに残し、整合性監査バッチで補完。
    record_upload_succeeded = False
    try:
        record_upload(owner, size)
        record_upload_succeeded = True
    except Exception as e:
        from flask import current_app
        current_app.logger.exception(
            "api ai/drafts: record_upload failed (user=%d size=%d): %s",
            owner.id, size, e,
        )
    # record_upload 失敗時は加算が成立していないため TOCTOU 検証を
    # スキップする。検証走ると別ユーザーが先に上限近くまで埋めた状況で
    # 超過判定 → record_delete で他ユーザーの計上を誤減算する経路が
    # できてしまう。
    if record_upload_succeeded and get_used_bytes(owner) > get_quota_bytes(owner):
        from flask import current_app
        storage = get_storage_backend()
        for k in (key, make_thumbnail_key(key)):
            try:
                storage.delete(k)
            except Exception as e:
                current_app.logger.warning(
                    "api ai/drafts rollback: storage delete failed %s: %s",
                    k, e,
                )
        db.session.delete(draft)
        db.session.commit()
        try:
            record_delete(owner, size)
        except Exception as e:
            current_app.logger.exception(
                "api ai/drafts rollback: record_delete failed "
                "(user=%d size=%d): %s", owner.id, size, e,
            )
        return jsonify({
            "error": "並行アップロードにより容量上限を超えました。再試行してください。",
        }), 413

    # オプション: Webhook 通知
    if request.form.get("notify") == "1":
        _send_draft_notification(user_id, draft, suggestions_data)

    # 容量警告メール (Phase 6 #71)。閾値超過時のみ送信、失敗は best-effort
    maybe_send_quota_warning(owner)

    return jsonify({
        "ok": True,
        "draft_id": draft.id,
        "suggestions": suggestions_data,
    }), 201


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
    """E2 PR-C-4a: クライアント側で Round 1+2 プロンプトを組み立てるための
    材料をサーバから一括取得する endpoint。

    クライアントは:
      1. 画像 + round1_prompt + (compliance/custom_prompt 追記) で LLM 呼出
      2. Round 1 結果から needs_ledger=true なら ledger 取得 endpoint (別 PR)
      3. account_list_text + ledger + custom_prompt + round2_prompt_template で
         Round 2 プロンプト構築 → 2 度目の LLM 呼出
      4. PATCH /api/v1/ai/drafts/<id>/suggestions で結果保存

    本 endpoint はサーバ側 ai_receipt.py の DOCUMENT_PROMPT / COMPLIANCE_CHECK_PROMPT /
    _get_account_list_text / _build_suggestion_prompt と等価のメタデータを返却し、
    Round 2 プロンプト構築テンプレートは {account_list_text} / {ledger_section} /
    {custom_section} のプレースホルダ付きで返す。
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

    # Round 2 プロンプトテンプレート。
    # _build_suggestion_prompt にプレースホルダ文字列を渡してテンプレート
    # 生成する。クライアントは以下 2 つのプレースホルダを実行時に置換:
    #   __ACCOUNT_LIST_TEXT__ → 別途返却する account_list_text で置換
    #   __LEDGER_TEXT__       → Round 1 後に取得した ledger 文字列で置換
    # custom_prompt はサーバ側で既に埋め込み済み (再置換不要)。
    round2_prompt_template = _build_suggestion_prompt(
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
        # Round 2 プロンプト (account_list_text / ledger_text 埋め込み済テンプレート)
        # クライアントは __LEDGER_TEXT__ プレースホルダを実 ledger で置換するだけで完成
        "round2_prompt_template": round2_prompt_template,
        # account_list_text は今回 round2_prompt_template に既に埋め込み済みだが、
        # クライアント側でバリデーション (account_code が存在するか) 用に別途返却
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


# E2 PR-C-2: クライアント側 LLM 呼出フロー用 endpoint。
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


# --- レポート API (read-only) ---


@bp.route("/reports/trial-balance", methods=["GET"])
@api_key_required(scope="reports:read")
def api_trial_balance():
    """試算表 API

    Query: year (default: 当年), period_from (0-16, default: 0),
    period_to (0-16, default: 15)
    """
    from app.services.reports import compute_trial_balance

    year = request.args.get("year", type=int) or date_type.today().year
    pf = request.args.get("period_from", 0, type=int)
    pt = request.args.get("period_to", 15, type=int)

    balances = compute_trial_balance(g.api_user_id, year, pf, pt)
    return jsonify({
        "ok": True,
        "year": year,
        "period_from": max(0, min(16, pf)),
        "period_to": max(pf, min(16, pt)),
        "balances": balances,
    })


@bp.route("/reports/income-statement", methods=["GET"])
@api_key_required(scope="reports:read")
def api_income_statement():
    """損益計算書 API

    Query: year (default: 当年), month (1-12, optional)
    """
    from app.services.reports import compute_income_statement

    year = request.args.get("year", type=int) or date_type.today().year
    month = request.args.get("month", type=int)
    if month is not None and not (1 <= month <= 12):
        return jsonify({"error": "month は 1-12 で指定してください。"}), 400

    data = compute_income_statement(g.api_user_id, year, month)
    return jsonify({"ok": True, "year": year, "month": month, **data})


@bp.route("/reports/monthly", methods=["GET"])
@api_key_required(scope="reports:read")
def api_monthly_comparison():
    """月次比較 API

    Query: year (default: 当年)
    """
    from app.services.tax import get_monthly_comparison

    year = request.args.get("year", type=int) or date_type.today().year
    data = get_monthly_comparison(g.api_user_id, year)
    return jsonify({
        "ok": True,
        "year": year,
        "expense_accounts": data["expense_accounts"],
        "income_accounts": data["income_accounts"],
        "expense_totals": [int(v) for v in data["expense_totals"]],
        "income_totals": [int(v) for v in data["income_totals"]],
    })


@bp.route("/reports/tax", methods=["GET"])
@api_key_required(scope="reports:read")
def api_tax_summary():
    """確定申告集計 API

    Query: year (default: 当年)
    """
    from app.services.tax import get_tax_summary, get_medical_summary

    year = request.args.get("year", type=int) or date_type.today().year
    tax = get_tax_summary(g.api_user_id, year)
    medical = get_medical_summary(g.api_user_id, year)

    tax_serializable = {
        cat: {
            "label": v["label"],
            "total": int(v["total"]),
            "accounts": [
                {"name": a["name"], "amount": int(a["amount"])}
                for a in v["accounts"]
            ],
        }
        for cat, v in tax.items()
    }
    medical_serializable = {
        "total_paid": int(medical.get("total_paid", 0) or 0),
        "total_reimbursed": int(medical.get("total_reimbursed", 0) or 0),
        "net_total": int(medical.get("net_total", 0) or 0),
        "by_patient": [
            {
                "name": p.get("name", ""),
                "paid": int(p.get("paid", 0) or 0),
                "reimbursed": int(p.get("reimbursed", 0) or 0),
                "net": int(p.get("net", 0) or 0),
                "hospitals": [
                    {
                        "name": h.get("name", ""),
                        "paid": int(h.get("paid", 0) or 0),
                        "reimbursed": int(h.get("reimbursed", 0) or 0),
                        "net": int(h.get("net", 0) or 0),
                        "provider_type": h.get("provider_type", ""),
                    }
                    for h in p.get("hospitals", [])
                ],
            }
            for p in medical.get("by_patient", [])
        ],
    }
    return jsonify({
        "ok": True,
        "year": year,
        "tax_summary": tax_serializable,
        "medical_summary": medical_serializable,
    })


def _send_draft_notification(user_id: int, draft: AIDraft, suggestions: list):
    """下書き作成をWebhookで通知する"""
    from flask import current_app
    from app.models.auto_import import WebhookConfig
    from app.services.notify import send_webhook

    webhooks = WebhookConfig.query.filter_by(
        user_id=user_id, is_active=True
    ).all()
    if not webhooks:
        return

    base_url = current_app.config.get("WEBAUTHN_ORIGIN", "").rstrip("/")
    drafts_url = f"{base_url}/ai-journal/drafts" if base_url else None

    # 最初の候補からサマリーを生成
    title_parts = []
    if suggestions:
        s = suggestions[0]
        if s.get("date"):
            title_parts.append(s["date"])
        if s.get("entry_description"):
            title_parts.append(s["entry_description"])
    summary = " ".join(title_parts) if title_parts else "新しい下書き"

    message = f"AI証憑仕訳の下書きを作成しました。\n{summary}"

    for webhook in webhooks:
        events = json.loads(webhook.events_json)
        if "import_success" in events:
            message_id = send_webhook(
                provider=webhook.provider,
                url=webhook.webhook_url,
                title="いいかんじ™家計簿 AI仕訳",
                message=message,
                details={
                    "候補数": len(suggestions),
                    "メモ": draft.comment or "—",
                },
                link_url=drafts_url,
            )
            if message_id and webhook.provider == "discord":
                draft.discord_webhook_url = webhook.webhook_url
                draft.discord_message_id = message_id
                db.session.commit()


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
