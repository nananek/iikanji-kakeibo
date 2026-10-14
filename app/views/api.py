"""外部 API (Bearer APIキー認証)"""

import functools
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, g

from app.extensions import db, limiter
from app.models.api_key import APIKey
from app.models.oauth import OAuthToken
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.services import api_ops
from app.services.api_ops import ApiError
from app.services.image import serve_image
from app.services.storage import (
    get_storage_backend, make_storage_key, make_thumbnail_key,
    store_image_with_thumbnail, is_declared_image_valid,
)
from app.services.storage_quota import (
    QuotaExceededError, check_quota, is_over_quota,
    maybe_send_quota_warning, record_delete, record_upload,
)
from app.services.voucher import create_voucher_from_draft
from app.models.user import User
from app.models.voucher import Voucher
from app.views.helpers import safe_user_error

bp = Blueprint("api", __name__, url_prefix="/api/v1")


class BearerPrincipal:
    """Bearer トークンから解決した主体 (ユーザー + 権限)。"""

    def __init__(self, *, user_id, scopes, read_only, kind):
        self.user_id = user_id
        self.scopes = scopes          # None = 全スコープ (従来互換)
        self.read_only = read_only
        self.kind = kind              # "oauth" | "api_key"

    def has_scope(self, scope: str) -> bool:
        return self.scopes is None or scope in self.scopes


def resolve_bearer_principal():
    """Authorization: Bearer ヘッダーを解決する。

    API キー (``ik_*``) と OAuth トークン (``ikt_*``) の両方を受け入れる。

    Returns:
        ``(principal, None)`` または ``(None, (response, status))``
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, (jsonify({"error": "Authorization ヘッダーが必要です。"}), 401)

    raw = auth[7:]
    now = datetime.now(timezone.utc)

    # OAuth トークン
    if raw.startswith("ikt_"):
        token = OAuthToken.query.filter_by(
            token_hash=OAuthToken.hash_token(raw), is_active=True
        ).first()
        if not token:
            return None, (jsonify({"error": "無効なトークンです。"}), 401)
        if token.is_expired():
            return None, (
                jsonify({"error": "トークンの有効期限が切れています。"}), 401
            )
        token.last_used_at = now
        db.session.commit()
        return BearerPrincipal(
            user_id=token.user_id, scopes=token.scope_set(),
            read_only=token.read_only, kind="oauth",
        ), None

    # 従来のAPIキー
    api_key = APIKey.query.filter_by(
        key_hash=APIKey.hash_key(raw), is_active=True
    ).first()
    if not api_key:
        return None, (jsonify({"error": "無効な API キーです。"}), 401)

    api_key.last_used_at = now
    db.session.commit()
    return BearerPrincipal(
        user_id=api_key.user_id, scopes=set(api_key.scope_list),
        read_only=False, kind="api_key",
    ), None


def api_key_required(scope=None, write=False):
    """Authorization: Bearer <token> ヘッダーで認証するデコレータ

    APIキー (ik_*) と OAuth トークン (ikt_*) の両方を受け入れる。
    OAuth トークンはスコープ集合を持ち (未設定トークンは全スコープ)、
    scope を指定すると保持スコープを追加チェックする。
    write=True を指定したエンドポイントは、read_only な OAuth トークンからは拒否される。
    """

    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            principal, err = resolve_bearer_principal()
            if err is not None:
                return err

            if scope and not principal.has_scope(scope):
                if principal.kind == "api_key":
                    msg = f"この API キーには {scope} 権限がありません。"
                else:
                    msg = f"このトークンには {scope} 権限がありません。"
                return jsonify({"error": msg}), 403

            if write and principal.read_only:
                return (
                    jsonify({"error": "このトークンは読み取り専用です。"}),
                    403,
                )

            g.api_user_id = principal.user_id
            g.api_principal = principal
            return f(*args, **kwargs)

        return decorated

    return decorator


# --- 仕訳起票 ---


@bp.route("/journals", methods=["POST"])
@api_key_required(scope="journals:create", write=True)
def create_journal():
    """仕訳起票 API"""
    data = request.get_json(silent=True)
    user_id = g.api_user_id

    try:
        entry = api_ops.create_journal(user_id, data)
    except ApiError as e:
        from flask import current_app
        current_app.logger.exception("create_journal failed (API)")
        return jsonify({"error": e.message}), e.status

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


@bp.route("/journals", methods=["GET"])
@api_key_required(scope="journals:read")
def list_journals():
    """仕訳一覧 API"""
    try:
        result = api_ops.list_journals(
            g.api_user_id,
            page=request.args.get("page", 1, type=int),
            per_page=request.args.get("per_page", 20, type=int),
            date_from=request.args.get("date_from"),
            date_to=request.args.get("date_to"),
        )
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify(result)


@bp.route("/journals/<int:entry_id>", methods=["GET"])
@api_key_required(scope="journals:read")
def get_journal(entry_id):
    """仕訳詳細 API"""
    try:
        result = api_ops.get_journal(g.api_user_id, entry_id)
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify(result)


# --- 仕訳削除 ---


@bp.route("/journals/<int:entry_id>", methods=["DELETE"])
@api_key_required(scope="journals:delete", write=True)
def delete_journal(entry_id):
    """仕訳削除 API"""
    try:
        api_ops.delete_journal(g.api_user_id, entry_id)
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
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
    image_file = request.files.get("image")
    if not image_file or not image_file.filename:
        return jsonify({"error": "image は必須です。"}), 400

    try:
        draft, suggestions_data = _perform_analyze(
            g.api_user_id,
            image_file.read(),
            image_file.content_type,
            request.form.get("comment") or "",
            notify=request.form.get("notify") == "1",
        )
    except ApiError as e:
        return jsonify({"error": e.message}), e.status

    return jsonify({
        "ok": True,
        "draft_id": draft.id,
        "suggestions": suggestions_data,
    }), 201


def _perform_analyze(user_id, image_bytes, mime_type, comment, notify=False):
    """request 非依存の AI 解析コア (REST / MCP 共用)。

    Raises:
        ApiError: 設定不足・形式不正・容量超過など。

    Returns:
        (AIDraft, suggestions_data)
    """
    from app.services.ai_receipt import analyze_and_suggest

    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    if not config:
        raise ApiError("AI API設定が未登録です。")

    if len(image_bytes) > _MAX_IMAGE_SIZE:
        raise ApiError("ファイルサイズが大きすぎます（上限10MB）。")

    if mime_type not in _ALLOWED_MIME_TYPES:
        raise ApiError(
            "対応していないファイル形式です。JPEG/PNG/WebP/GIF を使用してください。"
        )
    if not is_declared_image_valid(image_bytes, mime_type):
        raise ApiError("画像ファイルの内容が形式と一致しません。")

    # Phase 5 #70: AIDraft 段階で StorageUsage に計上 (ai_journal.analyze と同等)
    size = len(image_bytes)
    owner = db.session.get(User, user_id)
    if owner is None:
        raise ApiError("ユーザーが見つかりません。")
    try:
        check_quota(owner, size)
    except QuotaExceededError as exc:
        # CodeQL py/stack-trace-exposure 対策で `exc.user_message` 経由 (PR #92)
        raise ApiError(exc.user_message, 413)

    file_hash = hashlib.sha256(image_bytes).hexdigest()
    comment = (comment or "").strip()[:500]

    try:
        suggestions = analyze_and_suggest(
            user_id, image_bytes, mime_type, comment=comment or None,
        )
    except (ValueError, RuntimeError) as e:
        from flask import current_app
        current_app.logger.exception("analyze_and_suggest failed (API)")
        raise ApiError(safe_user_error(e))

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
    if record_upload_succeeded and is_over_quota(owner):
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
        raise ApiError(
            "並行アップロードにより容量上限を超えました。再試行してください。", 413
        )

    # オプション: Webhook 通知
    if notify:
        _send_draft_notification(user_id, draft, suggestions_data)

    # 容量警告メール (Phase 6 #71)。閾値超過時のみ送信、失敗は best-effort
    maybe_send_quota_warning(owner)

    return draft, suggestions_data


@bp.route("/ai/drafts", methods=["GET"])
@api_key_required(scope="ai:analyze")
def ai_drafts():
    """下書き一覧 API"""
    try:
        result = api_ops.list_ai_drafts(
            g.api_user_id,
            status=request.args.get("status", "analyzed"),
            page=request.args.get("page", 1, type=int),
            per_page=request.args.get("per_page", 50, type=int),
        )
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify(result)


@bp.route("/ai/drafts/<int:draft_id>", methods=["GET"])
@api_key_required(scope="ai:analyze")
def ai_draft_detail(draft_id):
    """下書き詳細 API（候補含む）"""
    try:
        result = api_ops.get_ai_draft(g.api_user_id, draft_id)
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify(result)


@bp.route("/ai/drafts/<int:draft_id>", methods=["DELETE"])
@api_key_required(scope="ai:analyze", write=True)
def ai_draft_delete(draft_id):
    """下書き削除 API"""
    try:
        _perform_delete_draft(g.api_user_id, draft_id)
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify({"ok": True})


def _perform_delete_draft(user_id, draft_id):
    """request 非依存の下書き削除コア (REST / MCP 共用)。"""
    draft = AIDraft.query.filter_by(id=draft_id, user_id=user_id).first()
    if not draft or draft.status == "temp":
        raise ApiError("下書きが見つかりません。", 404)

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


@bp.route("/vouchers", methods=["GET"])
@api_key_required(scope="journals:read")
def list_vouchers():
    """証憑一覧 API"""
    try:
        result = api_ops.list_vouchers(
            g.api_user_id,
            page=request.args.get("page", 1, type=int),
            per_page=request.args.get("per_page", 20, type=int),
            date_from=request.args.get("date_from"),
            date_to=request.args.get("date_to"),
            search=request.args.get("search"),
            amount_from=request.args.get("amount_from", type=int),
            amount_to=request.args.get("amount_to", type=int),
        )
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify(result)


@bp.route("/vouchers/<int:voucher_id>/image", methods=["GET"])
@api_key_required(scope="journals:read")
def api_voucher_image(voucher_id):
    """証憑画像取得 API"""
    voucher = Voucher.query.filter_by(
        id=voucher_id, user_id=g.api_user_id
    ).first()
    if not voucher:
        return jsonify({"error": "証憑が見つかりません。"}), 404
    try:
        return serve_image(voucher.image_key, voucher.image_mime, voucher.file_hash)
    except FileNotFoundError:
        return jsonify({"error": "画像ファイルが見つかりません。"}), 404


# --- レポート API (read-only) ---


@bp.route("/reports/trial-balance", methods=["GET"])
@api_key_required(scope="reports:read")
def api_trial_balance():
    """試算表 API

    Query: year (default: 当年), period_from (0-16, default: 0),
    period_to (0-16, default: 15)
    """
    try:
        result = api_ops.trial_balance(
            g.api_user_id,
            year=request.args.get("year", type=int),
            period_from=request.args.get("period_from", 0, type=int),
            period_to=request.args.get("period_to", 15, type=int),
        )
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify(result)


@bp.route("/reports/income-statement", methods=["GET"])
@api_key_required(scope="reports:read")
def api_income_statement():
    """損益計算書 API

    Query: year (default: 当年), month (1-12, optional)
    """
    try:
        result = api_ops.income_statement(
            g.api_user_id,
            year=request.args.get("year", type=int),
            month=request.args.get("month", type=int),
        )
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify(result)


@bp.route("/reports/monthly", methods=["GET"])
@api_key_required(scope="reports:read")
def api_monthly_comparison():
    """月次比較 API

    Query: year (default: 当年)
    """
    try:
        result = api_ops.monthly_comparison(
            g.api_user_id, year=request.args.get("year", type=int)
        )
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify(result)


@bp.route("/reports/tax", methods=["GET"])
@api_key_required(scope="reports:read")
def api_tax_summary():
    """確定申告集計 API

    Query: year (default: 当年)
    """
    try:
        result = api_ops.tax_summary(
            g.api_user_id, year=request.args.get("year", type=int)
        )
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify(result)


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
