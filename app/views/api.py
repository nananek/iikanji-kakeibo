"""外部 API (Bearer APIキー認証)"""

import functools
import hashlib
import json
from dataclasses import asdict
from datetime import date as date_type, datetime, timezone

from flask import Blueprint, jsonify, request, g

from app.extensions import db, limiter
from app.models.api_key import APIKey
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.journal import JournalEntry
from app.services.accounting import create_journal_entry
from app.services.audit import get_submitted_account_ids, is_entry_locked_for_owner
from app.services.fiscal import check_period_open_for_new, check_entry_modifiable
from app.services.storage import get_storage_backend, make_storage_key
from app.services.voucher import create_voucher_from_draft
from app.models.voucher import Voucher

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def api_key_required(scope=None):
    """Authorization: Bearer ik_xxx ヘッダーで認証するデコレータ

    scope を指定すると、API キーにそのスコープがあるか追加チェックする。
    """

    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return jsonify({"error": "Authorization ヘッダーが必要です。"}), 401

            raw_key = auth[7:]
            key_hash = APIKey.hash_key(raw_key)
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

            api_key.last_used_at = datetime.now(timezone.utc)
            db.session.commit()

            g.api_user_id = api_key.user_id
            return f(*args, **kwargs)

        return decorated

    return decorator


# --- 仕訳起票 ---


@bp.route("/journals", methods=["POST"])
@api_key_required(scope="journals:create")
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
        account_id = line.get("account_id")
        if not account_id:
            return jsonify({"error": f"lines[{i}].account_id は必須です。"}), 400
        lines_data.append({
            "account_id": int(account_id),
            "debit_amount": int(line.get("debit", 0) or 0),
            "credit_amount": int(line.get("credit", 0) or 0),
            "description": line.get("description", ""),
        })

    # 提出済みロック科目チェック
    locked_ids = get_submitted_account_ids(user_id)
    if locked_ids:
        used_ids = {ld["account_id"] for ld in lines_data}
        if used_ids & locked_ids:
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
        return jsonify({"error": str(e)}), 400

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
                "account_id": line.account_id,
                "debit": line.debit_amount,
                "credit": line.credit_amount,
                "description": line.description or "",
            }
            for line in entry.lines
        ],
        "vouchers": [
            {
                "id": v.id,
                "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
            }
            for v in entry.vouchers
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
@api_key_required(scope="journals:delete")
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

    db.session.delete(entry)
    db.session.commit()

    return jsonify({"ok": True})


# --- AI 証憑仕訳 ---


_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
_ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@bp.route("/ai/analyze", methods=["POST"])
@limiter.limit("30/hour")
@api_key_required(scope="ai:analyze")
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

    file_hash = hashlib.sha256(image_bytes).hexdigest()
    comment = (request.form.get("comment") or "").strip()[:500]

    try:
        suggestions = analyze_and_suggest(
            user_id, image_bytes, mime_type, comment=comment or None,
        )
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400

    suggestions_data = [asdict(s) for s in suggestions]
    suggestions_json = json.dumps(suggestions_data, ensure_ascii=False)

    draft = AIDraft(
        user_id=user_id,
        image_key="",
        image_mime=mime_type,
        file_hash=file_hash,
        comment=comment or None,
        suggestions_json=suggestions_json,
        status="analyzed",
    )
    db.session.add(draft)
    db.session.flush()
    key = make_storage_key(draft.user_id, draft.id, mime_type)
    get_storage_backend().put(key, image_bytes, mime_type)
    draft.image_key = key
    db.session.commit()

    # オプション: Webhook 通知
    if request.form.get("notify") == "1":
        _send_draft_notification(user_id, draft, suggestions_data)

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
@api_key_required(scope="ai:analyze")
def ai_draft_delete(draft_id):
    """下書き削除 API"""
    draft = AIDraft.query.filter_by(
        id=draft_id, user_id=g.api_user_id
    ).first()
    if not draft or draft.status == "temp":
        return jsonify({"error": "下書きが見つかりません。"}), 404

    image_key = draft.image_key
    db.session.delete(draft)
    db.session.commit()
    get_storage_backend().delete(image_key)

    return jsonify({"ok": True})


@bp.route("/vouchers/<int:voucher_id>/image", methods=["GET"])
@api_key_required(scope="journals:read")
def api_voucher_image(voucher_id):
    """証憑画像取得 API"""
    from flask import Response
    voucher = Voucher.query.filter_by(
        id=voucher_id, user_id=g.api_user_id
    ).first()
    if not voucher:
        return jsonify({"error": "証憑が見つかりません。"}), 404
    try:
        image_data = get_storage_backend().get(voucher.image_key)
    except FileNotFoundError:
        return jsonify({"error": "画像ファイルが見つかりません。"}), 404
    return Response(image_data, mimetype=voucher.image_mime)


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
