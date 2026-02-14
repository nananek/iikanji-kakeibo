"""AI証憑仕訳ビュー"""

import json
from datetime import date as date_type

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, session,
)
from flask_login import login_required, current_user

from app.models.account import Account, AccountType
from app.models.ai_config import UserAIConfig
from app.services.ai_receipt import analyze_receipt, match_account
from app.services.accounting import create_journal_entry

bp = Blueprint("ai_journal", __name__, url_prefix="/ai-journal")

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _get_account_choices(user_id):
    """全勘定科目の選択肢"""
    accounts = (
        Account.query
        .filter_by(user_id=user_id, is_active=True)
        .order_by(Account.code)
        .all()
    )
    return [(a.id, f"{a.code} {a.name}") for a in accounts]


def _get_payment_choices(user_id):
    """資産・負債の勘定科目（支払元）の選択肢"""
    type_ids = [
        t.id for t in AccountType.query.filter(
            AccountType.code.in_(["asset", "liability"])
        ).all()
    ]
    accounts = (
        Account.query
        .filter(
            Account.user_id == user_id,
            Account.is_active.is_(True),
            Account.account_type_id.in_(type_ids),
        )
        .order_by(Account.code)
        .all()
    )
    return [(a.id, a.name) for a in accounts]


def _get_category_choices(user_id):
    """費用・収益の勘定科目（費目）の選択肢"""
    expense_type = AccountType.query.filter_by(code="expense").first()
    revenue_type = AccountType.query.filter_by(code="revenue").first()

    choices = []
    if expense_type:
        expenses = (
            Account.query
            .filter(
                Account.user_id == user_id,
                Account.is_active.is_(True),
                Account.account_type_id == expense_type.id,
            )
            .order_by(Account.code)
            .all()
        )
        choices.extend([(a.id, f"[支出] {a.name}") for a in expenses])

    if revenue_type:
        revenues = (
            Account.query
            .filter(
                Account.user_id == user_id,
                Account.is_active.is_(True),
                Account.account_type_id == revenue_type.id,
            )
            .order_by(Account.code)
            .all()
        )
        choices.extend([(a.id, f"[収入] {a.name}") for a in revenues])

    return choices


@bp.route("/", methods=["GET", "POST"])
@login_required
def upload():
    """Step 1: 証憑画像アップロード + AI解析"""
    config = UserAIConfig.query.filter_by(user_id=current_user.id).first()
    payment_choices = _get_payment_choices(current_user.id)

    if request.method == "POST":
        if not config:
            flash("AI API設定が登録されていません。先に設定してください。", "danger")
            return redirect(url_for("settings.ai_config"))

        image_file = request.files.get("image_file")
        if not image_file or not image_file.filename:
            flash("画像ファイルを選択してください。", "danger")
            return render_template(
                "ai_journal/upload.html",
                has_config=bool(config),
                payment_choices=payment_choices,
            )

        image_bytes = image_file.read()
        if len(image_bytes) > MAX_IMAGE_SIZE:
            flash("ファイルサイズが大きすぎます（上限10MB）。", "danger")
            return render_template(
                "ai_journal/upload.html",
                has_config=bool(config),
                payment_choices=payment_choices,
            )

        mime_type = image_file.content_type
        if mime_type not in ALLOWED_MIME_TYPES:
            flash(
                "対応していないファイル形式です。JPEG/PNG/WebP/GIF を使用してください。",
                "danger",
            )
            return render_template(
                "ai_journal/upload.html",
                has_config=bool(config),
                payment_choices=payment_choices,
            )

        payment_account_id = request.form.get("payment_account_id", type=int)
        if not payment_account_id:
            flash("支払元の口座を選択してください。", "danger")
            return render_template(
                "ai_journal/upload.html",
                has_config=bool(config),
                payment_choices=payment_choices,
            )

        try:
            result = analyze_receipt(current_user.id, image_bytes, mime_type)
        except (ValueError, RuntimeError) as e:
            flash(str(e), "danger")
            return render_template(
                "ai_journal/upload.html",
                has_config=bool(config),
                payment_choices=payment_choices,
            )

        matched_account_id = match_account(
            current_user.id, result.suggested_category
        )

        session["ai_journal_result"] = json.dumps(
            {
                "date": result.date,
                "description": result.description,
                "amount": result.amount,
                "suggested_category": result.suggested_category,
                "matched_account_id": matched_account_id,
                "payment_account_id": payment_account_id,
            },
            ensure_ascii=False,
        )

        return redirect(url_for("ai_journal.review"))

    return render_template(
        "ai_journal/upload.html",
        has_config=bool(config),
        payment_choices=payment_choices,
    )


@bp.route("/review", methods=["GET", "POST"])
@login_required
def review():
    """Step 2: AI結果の確認・編集・保存"""
    result_json = session.get("ai_journal_result")
    if not result_json:
        flash("AI解析データがありません。もう一度アップロードしてください。", "warning")
        return redirect(url_for("ai_journal.upload"))

    result = json.loads(result_json)
    payment_choices = _get_payment_choices(current_user.id)
    category_choices = _get_category_choices(current_user.id)
    account_choices = _get_account_choices(current_user.id)

    if request.method == "POST":
        mode = request.form.get("mode", "simple")
        entry_date_str = request.form.get("date", "")
        description = request.form.get("description", "").strip()

        if not entry_date_str or not description:
            flash("日付と摘要を入力してください。", "danger")
            return render_template(
                "ai_journal/review.html",
                result=result,
                payment_choices=payment_choices,
                category_choices=category_choices,
                account_choices=account_choices,
            )

        try:
            entry_date = date_type.fromisoformat(entry_date_str)
        except ValueError:
            flash("日付の形式が不正です。", "danger")
            return render_template(
                "ai_journal/review.html",
                result=result,
                payment_choices=payment_choices,
                category_choices=category_choices,
                account_choices=account_choices,
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
                    result=result,
                    payment_choices=payment_choices,
                    category_choices=category_choices,
                    account_choices=account_choices,
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
                    result=result,
                    payment_choices=payment_choices,
                    category_choices=category_choices,
                    account_choices=account_choices,
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
                result=result,
                payment_choices=payment_choices,
                category_choices=category_choices,
                account_choices=account_choices,
            )

        try:
            entry = create_journal_entry(
                user_id=current_user.id,
                date=entry_date,
                description=description,
                lines_data=lines_data,
                source="ai_receipt",
            )
            session.pop("ai_journal_result", None)
            flash(f"伝票 #{entry.entry_number} を登録しました。", "success")
            return redirect(url_for("journal.index"))
        except ValueError as e:
            flash(str(e), "danger")

    return render_template(
        "ai_journal/review.html",
        result=result,
        payment_choices=payment_choices,
        category_choices=category_choices,
        account_choices=account_choices,
    )
