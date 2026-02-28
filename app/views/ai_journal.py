"""AI証憑仕訳ビュー"""

import hashlib
import json
from dataclasses import asdict
from datetime import date as date_type

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, session,
    jsonify,
)
from flask_login import login_required, current_user

from app.extensions import db
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.services.audit import get_effective_user_id, get_submitted_account_ids
from app.services.ai_receipt import analyze_and_suggest
from app.services.accounting import create_journal_entry
from app.services.fiscal import check_period_open_for_new, get_closed_periods_map, get_restricted_before_year
from app.services.storage import get_storage_backend, make_storage_key
from app.services.voucher import create_voucher_from_draft
from app.models.voucher import Voucher
from app.views.helpers import get_grouped_accounts

bp = Blueprint("ai_journal", __name__, url_prefix="/ai-journal")

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@bp.route("/", methods=["GET"])
@login_required
def upload():
    """証憑画像アップロード画面"""
    user_id = get_effective_user_id()
    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    draft_count = AIDraft.query.filter_by(
        user_id=user_id, status="analyzed"
    ).count()
    # 前回の一時ドラフトをクリーンアップ
    temp_drafts = AIDraft.query.filter_by(user_id=user_id, status="temp").all()
    keys_to_delete = [d.image_key for d in temp_drafts]
    for d in temp_drafts:
        db.session.delete(d)
    db.session.commit()
    storage = get_storage_backend()
    for key in keys_to_delete:
        storage.delete(key)
    session.pop("ai_journal_draft_id", None)
    return render_template(
        "ai_journal/upload.html",
        has_config=bool(config),
        draft_count=draft_count,
    )


@bp.route("/analyze", methods=["POST"])
@login_required
def analyze():
    """AJAX: 証憑画像を解析して仕訳案を返す"""
    config = UserAIConfig.query.filter_by(user_id=get_effective_user_id()).first()
    if not config:
        return jsonify({"error": "AI API設定が登録されていません。先に設定してください。"}), 400

    image_file = request.files.get("image_file")
    if not image_file or not image_file.filename:
        return jsonify({"error": "画像ファイルを選択してください。"}), 400

    image_bytes = image_file.read()
    if len(image_bytes) > MAX_IMAGE_SIZE:
        return jsonify({"error": "ファイルサイズが大きすぎます（上限10MB）。"}), 400

    mime_type = image_file.content_type
    if mime_type not in ALLOWED_MIME_TYPES:
        return jsonify({
            "error": "対応していないファイル形式です。JPEG/PNG/WebP/GIF を使用してください。"
        }), 400

    file_hash = hashlib.sha256(image_bytes).hexdigest()
    comment = request.form.get("comment", "").strip()

    try:
        suggestions = analyze_and_suggest(
            get_effective_user_id(), image_bytes, mime_type, comment=comment
        )
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400

    suggestions_data = [asdict(s) for s in suggestions]
    suggestions_json = json.dumps(suggestions_data, ensure_ascii=False)

    # 画像・解析結果を保存（セッションには小さなIDのみ）
    draft = AIDraft(
        user_id=get_effective_user_id(),
        image_key="",
        image_mime=mime_type,
        file_hash=file_hash,
        comment=comment,
        suggestions_json=suggestions_json,
        status="temp",
    )
    db.session.add(draft)
    db.session.flush()
    key = make_storage_key(draft.user_id, draft.id, mime_type)
    get_storage_backend().put(key, image_bytes, mime_type)
    draft.image_key = key
    db.session.commit()
    session["ai_journal_draft_id"] = draft.id

    return jsonify({"suggestions": suggestions_data})


# --- 一時保存 ---

@bp.route("/drafts/save", methods=["POST"])
@login_required
def drafts_save():
    """AJAX: AI解析結果を一時保存する（temp → analyzed に昇格）"""
    draft_id = session.get("ai_journal_draft_id")
    if not draft_id:
        return jsonify({"error": "保存するデータがありません。"}), 400

    draft = AIDraft.query.get(draft_id)
    if not draft or draft.user_id != get_effective_user_id():
        return jsonify({"error": "保存するデータがありません。"}), 400

    draft.status = "analyzed"
    db.session.commit()

    session.pop("ai_journal_draft_id", None)
    return jsonify({"ok": True, "draft_id": draft.id})


@bp.route("/drafts")
@login_required
def drafts():
    """一時保存一覧"""
    user_id = get_effective_user_id()
    items = (
        AIDraft.query
        .filter_by(user_id=user_id)
        .filter_by(status="analyzed")
        .order_by(AIDraft.created_at.desc())
        .all()
    )

    # 各ドラフトのサマリーを抽出
    draft_list = []
    for d in items:
        summary = {}
        if d.suggestions_json:
            try:
                suggestions = json.loads(d.suggestions_json)
                if suggestions:
                    s = suggestions[0]
                    summary = {
                        "title": s.get("title", ""),
                        "date": s.get("date", ""),
                        "description": s.get("entry_description", ""),
                        "count": len(suggestions),
                    }
                    # 合計金額を計算
                    total = sum(
                        l.get("debit_amount", 0) for l in s.get("lines", [])
                    )
                    summary["amount"] = total
                    # コンプライアンスチェック結果
                    compliance = s.get("compliance")
                    if isinstance(compliance, dict):
                        summary["compliance_status"] = compliance.get("status")
                        summary["compliance_warnings"] = compliance.get("warnings", [])
            except (json.JSONDecodeError, IndexError):
                pass
        draft_list.append({"draft": d, "summary": summary})

    return render_template("ai_journal/drafts.html", draft_list=draft_list)


@bp.route("/drafts/<int:draft_id>/image")
@login_required
def draft_image(draft_id):
    """ドラフトの画像を返す"""
    draft = AIDraft.query.get_or_404(draft_id)
    if draft.user_id != get_effective_user_id():
        return "", 403
    from flask import Response
    try:
        image_data = get_storage_backend().get(draft.image_key)
    except FileNotFoundError:
        return "", 404
    return Response(image_data, mimetype=draft.image_mime)


@bp.route("/voucher/<int:voucher_id>/image")
@login_required
def voucher_image(voucher_id):
    """証憑画像を返す"""
    voucher = Voucher.query.get_or_404(voucher_id)
    if voucher.user_id != get_effective_user_id():
        return "", 403
    from flask import Response
    try:
        image_data = get_storage_backend().get(voucher.image_key)
    except FileNotFoundError:
        return "", 404
    return Response(image_data, mimetype=voucher.image_mime)


@bp.route("/drafts/<int:draft_id>/delete", methods=["POST"])
@login_required
def drafts_delete(draft_id):
    """ドラフト削除"""
    draft = AIDraft.query.get_or_404(draft_id)
    if draft.user_id != get_effective_user_id():
        flash("権限がありません。", "danger")
        return redirect(url_for("ai_journal.drafts"))

    image_key = draft.image_key
    db.session.delete(draft)
    db.session.commit()
    get_storage_backend().delete(image_key)
    flash("下書きを削除しました。", "info")
    return redirect(url_for("ai_journal.drafts"))


@bp.route("/drafts/<int:draft_id>/review", methods=["GET"])
@login_required
def drafts_review(draft_id):
    """ドラフトからreview画面へ遷移"""
    draft = AIDraft.query.get_or_404(draft_id)
    if draft.user_id != get_effective_user_id():
        flash("権限がありません。", "danger")
        return redirect(url_for("ai_journal.drafts"))

    if not draft.suggestions_json:
        flash("解析データがありません。", "warning")
        return redirect(url_for("ai_journal.drafts"))

    session["ai_journal_draft_id"] = draft.id

    idx = request.args.get("idx", 0, type=int)
    return redirect(url_for("ai_journal.review", idx=idx))


# --- review ---

@bp.route("/review", methods=["GET", "POST"])
@login_required
def review():
    """仕訳案の確認・編集・保存"""
    draft_id = session.get("ai_journal_draft_id")
    draft = None
    if draft_id:
        draft = AIDraft.query.get(draft_id)
        if draft and draft.user_id != get_effective_user_id():
            draft = None

    if not draft or not draft.suggestions_json:
        session.pop("ai_journal_draft_id", None)
        flash("AI解析データがありません。もう一度アップロードしてください。", "warning")
        return redirect(url_for("ai_journal.upload"))

    suggestions_json = draft.suggestions_json

    suggestions = json.loads(suggestions_json)
    suggestion_index = request.args.get("idx", 0, type=int)
    if suggestion_index < 0 or suggestion_index >= len(suggestions):
        suggestion_index = 0

    selected = suggestions[suggestion_index]

    user_id = get_effective_user_id()
    grouped_accounts = get_grouped_accounts(user_id)
    closed_periods = get_closed_periods_map(user_id)
    restricted_before = get_restricted_before_year(user_id)
    is_saved_draft = draft.status == "analyzed"

    if request.method == "POST":
        mode = request.form.get("mode", "simple")
        entry_date_str = request.form.get("date", "")
        description = request.form.get("description", "").strip()

        if not entry_date_str or not description:
            flash("日付と摘要を入力してください。", "danger")
            return render_template(
                "ai_journal/review.html",
                suggestions=suggestions,
                selected=selected,
                selected_index=suggestion_index,
                grouped_accounts=grouped_accounts,
                draft_id=is_saved_draft,
                closed_periods=closed_periods,
                restricted_before_year=restricted_before,
            )

        try:
            entry_date = date_type.fromisoformat(entry_date_str)
        except ValueError:
            flash("日付の形式が不正です。", "danger")
            return render_template(
                "ai_journal/review.html",
                suggestions=suggestions,
                selected=selected,
                selected_index=suggestion_index,
                grouped_accounts=grouped_accounts,
                draft_id=is_saved_draft,
                closed_periods=closed_periods,
                restricted_before_year=restricted_before,
            )

        if mode == "simple":
            amount = request.form.get("amount", 0, type=int)
            category_id = request.form.get("category_account_id", 0, type=int)
            pay_account_id = request.form.get(
                "payment_account_id", 0, type=int
            )

            if amount <= 0 or not category_id or not pay_account_id:
                flash("金額・費目・支払元を入力してください。", "danger")
                return render_template(
                    "ai_journal/review.html",
                    suggestions=suggestions,
                    selected=selected,
                    selected_index=suggestion_index,
                    grouped_accounts=grouped_accounts,
                    draft_id=is_saved_draft,
                )

            lines_data = [
                {
                    "account_id": category_id,
                    "debit_amount": amount,
                    "credit_amount": 0,
                    "description": "",
                },
                {
                    "account_id": pay_account_id,
                    "debit_amount": 0,
                    "credit_amount": amount,
                    "description": "",
                },
            ]
        else:
            lines_json = request.form.get("lines_json", "[]")
            try:
                raw_lines = json.loads(lines_json)
            except json.JSONDecodeError:
                flash("明細データが不正です。", "danger")
                return render_template(
                    "ai_journal/review.html",
                    suggestions=suggestions,
                    selected=selected,
                    selected_index=suggestion_index,
                    grouped_accounts=grouped_accounts,
                    draft_id=is_saved_draft,
                )

            lines_data = [
                {
                    "account_id": int(line["account_id"]),
                    "debit_amount": int(line.get("debit_amount", 0) or 0),
                    "credit_amount": int(line.get("credit_amount", 0) or 0),
                    "description": line.get("description", ""),
                }
                for line in raw_lines
                if line.get("account_id")
            ]

        if not lines_data:
            flash("仕訳明細を入力してください。", "danger")
            return render_template(
                "ai_journal/review.html",
                suggestions=suggestions,
                selected=selected,
                selected_index=suggestion_index,
                grouped_accounts=grouped_accounts,
                draft_id=is_saved_draft,
                closed_periods=closed_periods,
                restricted_before_year=restricted_before,
            )

        # 提出済みロック科目チェック
        locked_ids = get_submitted_account_ids(get_effective_user_id())
        if locked_ids:
            used_ids = {line["account_id"] for line in lines_data}
            if used_ids & locked_ids:
                flash("提出済みの税務科目を含むため登録できません。", "danger")
                return render_template(
                    "ai_journal/review.html",
                    suggestions=suggestions,
                    selected=selected,
                    selected_index=suggestion_index,
                    grouped_accounts=grouped_accounts,
                    draft_id=is_saved_draft,
                )

        # 確定済み期間チェック
        err = check_period_open_for_new(
            get_effective_user_id(), entry_date.year, entry_date.month
        )
        if err:
            flash(err, "danger")
            return render_template(
                "ai_journal/review.html",
                suggestions=suggestions,
                selected=selected,
                selected_index=suggestion_index,
                grouped_accounts=grouped_accounts,
                draft_id=is_saved_draft,
                closed_periods=closed_periods,
                restricted_before_year=restricted_before,
            )

        try:
            entry = create_journal_entry(
                user_id=get_effective_user_id(),
                date=entry_date,
                description=description,
                lines_data=lines_data,
                source="ai_receipt",
            )
            session.pop("ai_journal_draft_id", None)
            _update_discord_done(draft, entry.entry_number)
            create_voucher_from_draft(draft, entry.id)
            db.session.commit()

            flash(f"伝票 #{entry.entry_number} を登録しました。", "success")

            if is_saved_draft:
                return redirect(url_for("ai_journal.drafts"))
            return redirect(url_for("journal.index"))
        except ValueError as e:
            flash(str(e), "danger")

    return render_template(
        "ai_journal/review.html",
        suggestions=suggestions,
        selected=selected,
        selected_index=suggestion_index,
        grouped_accounts=grouped_accounts,
        draft_id=is_saved_draft,
        closed_periods=closed_periods,
        restricted_before_year=restricted_before,
    )


def _update_discord_done(draft, entry_number):
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

    msg = ""
    if original_desc:
        msg += f"~~{original_desc}~~\n"
    msg += f"仕訳を登録しました（伝票 #{entry_number}）"

    update_discord_message(
        webhook_url=draft.discord_webhook_url,
        message_id=draft.discord_message_id,
        title="✅ いいかんじ™家計簿 AI仕訳",
        message=msg,
    )
