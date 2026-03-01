"""Webページ貼り付け→AI明細取込ビュー"""

import json
import uuid
from datetime import date as date_type

from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account, AccountType
from app.services.audit import get_effective_user_id, get_submitted_account_codes
from app.models.ai_config import UserAIConfig
from app.services.ai_receipt import parse_web_text
from app.services.accounting import create_cashbook_entry, create_transfer_entry
from app.services.fiscal import (
    check_period_open_for_new, get_restricted_before_year, is_year_open,
    get_capital_account_code, get_closed_periods_for_dates,
)
from app.views.helpers import (
    get_grouped_accounts, save_import_data, load_import_data, delete_import_data,
)

bp = Blueprint("web_import", __name__, url_prefix="/web-import")

MAX_TEXT_LENGTH = 200_000


@bp.route("/", methods=["GET", "POST"])
@login_required
def upload():
    """Step 1: テキスト入力 + 口座選択 → AI解析"""
    grouped_accounts = get_grouped_accounts(get_effective_user_id())
    has_config = UserAIConfig.query.filter_by(user_id=get_effective_user_id()).first() is not None

    if request.method == "POST":
        raw_text = request.form.get("raw_text", "").strip()
        payment_account_code = request.form.get("payment_account_code")

        if not raw_text:
            flash("テキストを入力してください。", "danger")
            return render_template(
                "web_import/upload.html",
                grouped_accounts=grouped_accounts,
                has_config=has_config,
            )

        if len(raw_text) > MAX_TEXT_LENGTH:
            flash("テキストが長すぎます（上限20万文字）。", "danger")
            return render_template(
                "web_import/upload.html",
                grouped_accounts=grouped_accounts,
                has_config=has_config,
            )

        if not payment_account_code:
            flash("取込先の口座を選択してください。", "danger")
            return render_template(
                "web_import/upload.html",
                grouped_accounts=grouped_accounts,
                has_config=has_config,
            )

        payment_account = Account.query.filter_by(user_id=get_effective_user_id(), code=payment_account_code).first()

        try:
            parsed = parse_web_text(
                get_effective_user_id(), raw_text, payment_account.name
            )
        except (ValueError, RuntimeError) as e:
            flash(str(e), "danger")
            return render_template(
                "web_import/upload.html",
                grouped_accounts=grouped_accounts,
                has_config=has_config,
                raw_text=raw_text,
                payment_account_code=payment_account_code,
                payment_account_name=payment_account.name,
            )

        if not parsed:
            flash("明細データを読み取れませんでした。テキストを確認してください。", "danger")
            return render_template(
                "web_import/upload.html",
                grouped_accounts=grouped_accounts,
                has_config=has_config,
                raw_text=raw_text,
                payment_account_code=payment_account_code,
                payment_account_name=payment_account.name,
            )

        key = save_import_data(parsed)
        session["web_data_key"] = key
        session["web_payment_account_code"] = payment_account_code

        return redirect(url_for("web_import.confirm"))

    return render_template(
        "web_import/upload.html",
        grouped_accounts=grouped_accounts,
        has_config=has_config,
    )


@bp.route("/confirm", methods=["GET", "POST"])
@login_required
def confirm():
    """Step 2: 確認して一括取込（日付一括編集対応）"""
    data_key = session.get("web_data_key")
    payment_account_code = session.get("web_payment_account_code")
    parsed = load_import_data(data_key)
    if not parsed or not payment_account_code:
        flash("データがありません。もう一度入力してください。", "warning")
        return redirect(url_for("web_import.upload"))
    payment_account = Account.query.filter_by(user_id=get_effective_user_id(), code=payment_account_code).first()

    expense_type = AccountType.query.filter_by(code="expense").first()
    default_expense = (
        Account.query
        .filter_by(user_id=get_effective_user_id(), account_type_id=expense_type.id, is_active=True)
        .order_by(Account.code)
        .first()
    )
    revenue_type = AccountType.query.filter_by(code="revenue").first()
    default_income = (
        Account.query
        .filter_by(user_id=get_effective_user_id(), account_type_id=revenue_type.id, is_active=True)
        .order_by(Account.code)
        .first()
    )

    # 振替判定用
    asset_type = AccountType.query.filter_by(code="asset").first()
    liability_type = AccountType.query.filter_by(code="liability").first()
    transfer_type_ids = {asset_type.id, liability_type.id}

    user_id = get_effective_user_id()
    restricted_before = get_restricted_before_year(user_id)
    capital_code = get_capital_account_code(user_id)

    if request.method == "POST":
        import_rows = request.form.get("import_rows", "")
        if not import_rows:
            flash("取り込むデータがありません。", "danger")
            return redirect(url_for("web_import.upload"))

        rows_data = json.loads(import_rows)
        old_year_action = request.form.get("old_year_action", "skip")
        imported = 0
        skipped = 0
        capital_count = 0
        batch_id = str(uuid.uuid4())
        locked_codes = get_submitted_account_codes(user_id)
        today_year = date_type.today().year

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
            category_code = row.get("category_code", "")

            if not category_code or (deposit == 0 and withdrawal == 0):
                skipped += 1
                continue

            # 提出済みロック科目チェック
            if locked_codes and {payment_account_code, category_code} & locked_codes:
                skipped += 1
                continue

            # 未開設年度チェック
            if restricted_before and row_date.year < restricted_before:
                if not is_year_open(user_id, row_date.year):
                    if old_year_action == "capital" and capital_code:
                        description = f"({row_date_str}) {description}"
                        row_date = date_type(today_year, 1, 1)
                        category_code = capital_code
                        capital_count += 1
                    else:
                        skipped += 1
                        continue

            # 確定済み期間チェック
            err = check_period_open_for_new(user_id, row_date.year, row_date.month)
            if err:
                skipped += 1
                continue

            cat_account = Account.query.filter_by(user_id=user_id, code=category_code).first()
            is_transfer = cat_account and cat_account.account_type_id in transfer_type_ids

            if is_transfer:
                amount = deposit or withdrawal
                if withdrawal > 0:
                    create_transfer_entry(
                        user_id=user_id,
                        date=row_date,
                        from_account_code=payment_account_code,
                        to_account_code=category_code,
                        amount=amount,
                        description=description,
                        batch_id=batch_id,
                    )
                else:
                    create_transfer_entry(
                        user_id=user_id,
                        date=row_date,
                        from_account_code=category_code,
                        to_account_code=payment_account_code,
                        amount=amount,
                        description=description,
                        batch_id=batch_id,
                    )
                imported += 1
            elif deposit > 0:
                create_cashbook_entry(
                    user_id=user_id,
                    date=row_date,
                    transaction_type="income",
                    payment_account_code=payment_account_code,
                    category_account_code=category_code,
                    amount=deposit,
                    description=description,
                    batch_id=batch_id,
                )
                imported += 1
            elif withdrawal > 0:
                create_cashbook_entry(
                    user_id=user_id,
                    date=row_date,
                    transaction_type="expense",
                    payment_account_code=payment_account_code,
                    category_account_code=category_code,
                    amount=withdrawal,
                    description=description,
                    batch_id=batch_id,
                )
                imported += 1
            else:
                skipped += 1

        delete_import_data(data_key)
        session.pop("web_data_key", None)
        session.pop("web_payment_account_code", None)

        msg = f"{imported}件を取り込みました。（スキップ: {skipped}件）"
        if capital_count:
            msg += f"（うち元入金変換: {capital_count}件）"
        flash(msg, "success")
        return redirect(url_for("cashbook.index"))

    closed_periods = get_closed_periods_for_dates(
        user_id, [r.get("date", "") for r in parsed]
    )
    grouped_accounts = get_grouped_accounts(user_id)
    has_ai_config = UserAIConfig.query.filter_by(user_id=user_id).first() is not None
    return render_template(
        "web_import/confirm.html",
        parsed=parsed,
        payment_account=payment_account,
        default_expense_id=default_expense.code if default_expense else 0,
        default_income_id=default_income.code if default_income else 0,
        grouped_accounts=grouped_accounts,
        restricted_before_year=restricted_before,
        closed_periods=closed_periods,
        has_ai_config=has_ai_config,
    )
