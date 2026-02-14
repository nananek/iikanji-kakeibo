"""AI証憑仕訳ビュー"""

import json
from dataclasses import asdict
from datetime import date as date_type

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, session,
    jsonify,
)
from flask_login import login_required, current_user

from app.models.ai_config import UserAIConfig
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
    config = UserAIConfig.query.filter_by(user_id=current_user.id).first()
    return render_template(
        "ai_journal/upload.html",
        has_config=bool(config),
    )


@bp.route("/analyze", methods=["POST"])
@login_required
def analyze():
    """AJAX: 証憑画像を解析して仕訳案を返す"""
    config = UserAIConfig.query.filter_by(user_id=current_user.id).first()
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

    try:
        suggestions = analyze_and_suggest(
            current_user.id, image_bytes, mime_type
        )
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400

    suggestions_data = [asdict(s) for s in suggestions]

    session["ai_journal_suggestions"] = json.dumps(
        suggestions_data, ensure_ascii=False
    )

    return jsonify({"suggestions": suggestions_data})


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

    grouped_accounts = get_grouped_accounts(current_user.id)

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
            )

        # 確定済み期間チェック
        err = check_period_open_for_new(
            current_user.id, entry_date.year, entry_date.month
        )
        if err:
            flash(err, "danger")
            return render_template(
                "ai_journal/review.html",
                suggestions=suggestions,
                selected=selected,
                selected_index=suggestion_index,
                grouped_accounts=grouped_accounts,
            )

        try:
            entry = create_journal_entry(
                user_id=current_user.id,
                date=entry_date,
                description=description,
                lines_data=lines_data,
                source="ai_receipt",
            )
            session.pop("ai_journal_suggestions", None)
            flash(f"伝票 #{entry.entry_number} を登録しました。", "success")
            return redirect(url_for("journal.index"))
        except ValueError as e:
            flash(str(e), "danger")

    return render_template(
        "ai_journal/review.html",
        suggestions=suggestions,
        selected=selected,
        selected_index=suggestion_index,
        grouped_accounts=grouped_accounts,
    )
