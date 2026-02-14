"""Webページ貼り付け→AI明細取込ビュー"""

import json
from datetime import date as date_type

from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.ai_config import UserAIConfig
from app.services.ai_receipt import parse_web_text
from app.services.accounting import create_cashbook_entry, create_transfer_entry

bp = Blueprint("web_import", __name__, url_prefix="/web-import")

MAX_TEXT_LENGTH = 200_000


def _get_payment_choices(user_id):
    asset_type = AccountType.query.filter_by(code="asset").first()
    liability_type = AccountType.query.filter_by(code="liability").first()
    accounts = (
        Account.query
        .filter(
            Account.user_id == user_id,
            Account.is_active.is_(True),
            Account.account_type_id.in_([asset_type.id, liability_type.id]),
        )
        .order_by(Account.code)
        .all()
    )
    return [(a.id, a.name) for a in accounts]


def _get_category_choices(user_id, exclude_account_id=None):
    expense_type = AccountType.query.filter_by(code="expense").first()
    revenue_type = AccountType.query.filter_by(code="revenue").first()
    asset_type = AccountType.query.filter_by(code="asset").first()
    liability_type = AccountType.query.filter_by(code="liability").first()

    def _query(type_id):
        q = Account.query.filter(
            Account.user_id == user_id,
            Account.is_active.is_(True),
            Account.account_type_id == type_id,
        )
        if exclude_account_id:
            q = q.filter(Account.id != exclude_account_id)
        return q.order_by(Account.code).all()

    expenses = _query(expense_type.id) if expense_type else []
    revenues = _query(revenue_type.id) if revenue_type else []
    assets = _query(asset_type.id) if asset_type else []
    liabilities = _query(liability_type.id) if liability_type else []

    return (
        [(a.id, f"【支出】{a.name}") for a in expenses]
        + [(a.id, f"【収入】{a.name}") for a in revenues]
        + [(a.id, f"【振替】{a.name}") for a in assets]
        + [(a.id, f"【振替】{a.name}") for a in liabilities]
    )


@bp.route("/", methods=["GET", "POST"])
@login_required
def upload():
    """Step 1: テキスト入力 + 口座選択 → AI解析"""
    payment_choices = _get_payment_choices(current_user.id)
    has_config = UserAIConfig.query.filter_by(user_id=current_user.id).first() is not None

    if request.method == "POST":
        raw_text = request.form.get("raw_text", "").strip()
        payment_account_id = request.form.get("payment_account_id", type=int)

        if not raw_text:
            flash("テキストを入力してください。", "danger")
            return render_template(
                "web_import/upload.html",
                payment_choices=payment_choices,
                has_config=has_config,
            )

        if len(raw_text) > MAX_TEXT_LENGTH:
            flash("テキストが長すぎます（上限20万文字）。", "danger")
            return render_template(
                "web_import/upload.html",
                payment_choices=payment_choices,
                has_config=has_config,
            )

        if not payment_account_id:
            flash("取込先の口座を選択してください。", "danger")
            return render_template(
                "web_import/upload.html",
                payment_choices=payment_choices,
                has_config=has_config,
            )

        payment_account = Account.query.get(payment_account_id)

        try:
            parsed = parse_web_text(
                current_user.id, raw_text, payment_account.name
            )
        except (ValueError, RuntimeError) as e:
            flash(str(e), "danger")
            return render_template(
                "web_import/upload.html",
                payment_choices=payment_choices,
                has_config=has_config,
                raw_text=raw_text,
                payment_account_id=payment_account_id,
            )

        if not parsed:
            flash("明細データを読み取れませんでした。テキストを確認してください。", "danger")
            return render_template(
                "web_import/upload.html",
                payment_choices=payment_choices,
                has_config=has_config,
                raw_text=raw_text,
                payment_account_id=payment_account_id,
            )

        session["web_parsed"] = json.dumps(parsed, ensure_ascii=False)
        session["web_payment_account_id"] = payment_account_id

        return redirect(url_for("web_import.confirm"))

    return render_template(
        "web_import/upload.html",
        payment_choices=payment_choices,
        has_config=has_config,
    )


@bp.route("/confirm", methods=["GET", "POST"])
@login_required
def confirm():
    """Step 2: 確認して一括取込（日付一括編集対応）"""
    parsed_json = session.get("web_parsed")
    payment_account_id = session.get("web_payment_account_id")
    if not parsed_json or not payment_account_id:
        flash("データがありません。もう一度入力してください。", "warning")
        return redirect(url_for("web_import.upload"))

    parsed = json.loads(parsed_json)
    payment_account = Account.query.get(payment_account_id)
    category_choices = _get_category_choices(current_user.id, exclude_account_id=payment_account_id)

    expense_type = AccountType.query.filter_by(code="expense").first()
    default_expense = (
        Account.query
        .filter_by(user_id=current_user.id, account_type_id=expense_type.id, is_active=True)
        .order_by(Account.code)
        .first()
    )
    revenue_type = AccountType.query.filter_by(code="revenue").first()
    default_income = (
        Account.query
        .filter_by(user_id=current_user.id, account_type_id=revenue_type.id, is_active=True)
        .order_by(Account.code)
        .first()
    )

    # 振替判定用
    asset_type = AccountType.query.filter_by(code="asset").first()
    liability_type = AccountType.query.filter_by(code="liability").first()
    transfer_type_ids = {asset_type.id, liability_type.id}

    if request.method == "POST":
        import_rows = request.form.get("import_rows", "")
        if not import_rows:
            flash("取り込むデータがありません。", "danger")
            return redirect(url_for("web_import.upload"))

        rows_data = json.loads(import_rows)
        imported = 0
        skipped = 0

        for row in rows_data:
            if not row.get("enabled", True):
                skipped += 1
                continue

            row_date_str = row.get("date")
            if not row_date_str:
                skipped += 1
                continue

            row_date = date_type.fromisoformat(row_date_str)
            description = row.get("description", "")
            deposit = int(row.get("deposit", 0))
            withdrawal = int(row.get("withdrawal", 0))
            category_id = int(row.get("category_id", 0))

            if not category_id or (deposit == 0 and withdrawal == 0):
                skipped += 1
                continue

            cat_account = Account.query.get(category_id)
            is_transfer = cat_account and cat_account.account_type_id in transfer_type_ids

            if is_transfer:
                amount = deposit or withdrawal
                if withdrawal > 0:
                    create_transfer_entry(
                        user_id=current_user.id,
                        date=row_date,
                        from_account_id=payment_account_id,
                        to_account_id=category_id,
                        amount=amount,
                        description=description,
                    )
                else:
                    create_transfer_entry(
                        user_id=current_user.id,
                        date=row_date,
                        from_account_id=category_id,
                        to_account_id=payment_account_id,
                        amount=amount,
                        description=description,
                    )
                imported += 1
            elif deposit > 0:
                create_cashbook_entry(
                    user_id=current_user.id,
                    date=row_date,
                    transaction_type="income",
                    payment_account_id=payment_account_id,
                    category_account_id=category_id,
                    amount=deposit,
                    description=description,
                )
                imported += 1
            elif withdrawal > 0:
                create_cashbook_entry(
                    user_id=current_user.id,
                    date=row_date,
                    transaction_type="expense",
                    payment_account_id=payment_account_id,
                    category_account_id=category_id,
                    amount=withdrawal,
                    description=description,
                )
                imported += 1
            else:
                skipped += 1

        session.pop("web_parsed", None)
        session.pop("web_payment_account_id", None)

        flash(f"{imported}件を取り込みました。（スキップ: {skipped}件）", "success")
        return redirect(url_for("cashbook.index"))

    return render_template(
        "web_import/confirm.html",
        parsed=parsed,
        payment_account=payment_account,
        category_choices=category_choices,
        default_expense_id=default_expense.id if default_expense else 0,
        default_income_id=default_income.id if default_income else 0,
    )
