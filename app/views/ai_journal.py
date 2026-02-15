"""AI証憑仕訳ビュー"""

import base64
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
from app.services.fiscal import check_period_open_for_new
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

    comment = request.form.get("comment", "").strip()

    try:
        suggestions = analyze_and_suggest(
            get_effective_user_id(), image_bytes, mime_type, comment=comment
        )
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400

    suggestions_data = [asdict(s) for s in suggestions]

    session["ai_journal_suggestions"] = json.dumps(
        suggestions_data, ensure_ascii=False
    )
    # 一時保存用に画像・コメントもセッションに退避
    session["ai_journal_image"] = base64.b64encode(image_bytes).decode("ascii")
    session["ai_journal_mime"] = mime_type
    session["ai_journal_comment"] = comment

    return jsonify({"suggestions": suggestions_data})


# --- 一時保存 ---

@bp.route("/drafts/save", methods=["POST"])
@login_required
def drafts_save():
    """AJAX: AI解析結果を一時保存する"""
    suggestions_json = session.get("ai_journal_suggestions")
    image_b64 = session.get("ai_journal_image")
    mime_type = session.get("ai_journal_mime")
    comment = session.get("ai_journal_comment", "")

    if not suggestions_json or not image_b64:
        return jsonify({"error": "保存するデータがありません。"}), 400

    draft = AIDraft(
        user_id=get_effective_user_id(),
        image_data=base64.b64decode(image_b64),
        image_mime=mime_type,
        comment=comment,
        suggestions_json=suggestions_json,
        status="analyzed",
    )
    db.session.add(draft)
    db.session.commit()

    # セッションクリア
    session.pop("ai_journal_suggestions", None)
    session.pop("ai_journal_image", None)
    session.pop("ai_journal_mime", None)
    session.pop("ai_journal_comment", None)

    return jsonify({"ok": True, "draft_id": draft.id})


@bp.route("/drafts")
@login_required
def drafts():
    """一時保存一覧"""
    user_id = get_effective_user_id()
    items = (
        AIDraft.query
        .filter_by(user_id=user_id)
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
    return Response(draft.image_data, mimetype=draft.image_mime)


@bp.route("/drafts/<int:draft_id>/delete", methods=["POST"])
@login_required
def drafts_delete(draft_id):
    """ドラフト削除"""
    draft = AIDraft.query.get_or_404(draft_id)
    if draft.user_id != get_effective_user_id():
        flash("権限がありません。", "danger")
        return redirect(url_for("ai_journal.drafts"))

    db.session.delete(draft)
    db.session.commit()
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

    # セッションにドラフトの仕訳案をロード
    session["ai_journal_suggestions"] = draft.suggestions_json
    session["ai_journal_draft_id"] = draft.id

    idx = request.args.get("idx", 0, type=int)
    return redirect(url_for("ai_journal.review", idx=idx))


# --- review ---

@bp.route("/review", methods=["GET", "POST"])
@login_required
def review():
    """仕訳案の確認・編集・保存"""
    suggestions_json = session.get("ai_journal_suggestions")
    if not suggestions_json:
        flash("AI解析データがありません。もう一度アップロードしてください。", "warning")
        return redirect(url_for("ai_journal.upload"))

    suggestions = json.loads(suggestions_json)
    suggestion_index = request.args.get("idx", 0, type=int)
    if suggestion_index < 0 or suggestion_index >= len(suggestions):
        suggestion_index = 0

    selected = suggestions[suggestion_index]

    grouped_accounts = get_grouped_accounts(get_effective_user_id())
    draft_id = session.get("ai_journal_draft_id")

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
                draft_id=draft_id,
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
                draft_id=draft_id,
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
                    draft_id=draft_id,
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
                    draft_id=draft_id,
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
                draft_id=draft_id,
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
                    draft_id=draft_id,
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
                draft_id=draft_id,
            )

        try:
            entry = create_journal_entry(
                user_id=get_effective_user_id(),
                date=entry_date,
                description=description,
                lines_data=lines_data,
                source="ai_receipt",
            )
            # ドラフト経由の場合、ステータスを更新
            if draft_id:
                draft = AIDraft.query.get(draft_id)
                if draft and draft.user_id == get_effective_user_id():
                    draft.status = "done"
                    db.session.commit()
                session.pop("ai_journal_draft_id", None)

            session.pop("ai_journal_suggestions", None)
            flash(f"伝票 #{entry.entry_number} を登録しました。", "success")

            # ドラフト経由ならドラフト一覧に戻る
            if draft_id:
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
        draft_id=draft_id,
    )
