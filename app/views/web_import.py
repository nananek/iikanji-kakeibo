"""Webページ貼り付け→AI明細取込ビュー"""

import json
import uuid
from datetime import date as date_type

from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account, AccountType
from app.services.audit import get_effective_user_id, get_submitted_account_ids
from app.models.ai_config import UserAIConfig
from app.services.ai_receipt import parse_web_text
from app.services.accounting import create_cashbook_entry, create_transfer_entry
from app.services.fiscal import check_period_open_for_new
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
        payment_account_id = request.form.get("payment_account_id", type=int)

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

        if not payment_account_id:
            flash("取込先の口座を選択してください。", "danger")
            return render_template(
                "web_import/upload.html",
                grouped_accounts=grouped_accounts,
                has_config=has_config,
            )

        payment_account = Account.query.get(payment_account_id)

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
                payment_account_id=payment_account_id,
                payment_account_name=payment_account.name,
            )

        if not parsed:
            flash("明細データを読み取れませんでした。テキストを確認してください。", "danger")
            return render_template(
                "web_import/upload.html",
                grouped_accounts=grouped_accounts,
                has_config=has_config,
                raw_text=raw_text,
                payment_account_id=payment_account_id,
                payment_account_name=payment_account.name,
            )

        key = save_import_data(parsed)
        session["web_data_key"] = key
        session["web_payment_account_id"] = payment_account_id

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
    payment_account_id = session.get("web_payment_account_id")
    parsed = load_import_data(data_key)
    if not parsed or not payment_account_id:
        flash("データがありません。もう一度入力してください。", "warning")
        return redirect(url_for("web_import.upload"))
    payment_account = Account.query.get(payment_account_id)

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

    if request.method == "POST":
        import_rows = request.form.get("import_rows", "")
        if not import_rows:
            flash("取り込むデータがありません。", "danger")
            return redirect(url_for("web_import.upload"))

        rows_data = json.loads(import_rows)
        imported = 0
        skipped = 0
        batch_id = str(uuid.uuid4())
        locked_ids = get_submitted_account_ids(get_effective_user_id())

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

            # 提出済みロック科目チェック
            if locked_ids and {payment_account_id, category_id} & locked_ids:
                skipped += 1
                continue

            # 確定済み期間チェック
            err = check_period_open_for_new(
                get_effective_user_id(), row_date.year, row_date.month
            )
            if err:
                skipped += 1
                continue

            cat_account = Account.query.get(category_id)
            is_transfer = cat_account and cat_account.account_type_id in transfer_type_ids

            if is_transfer:
                amount = deposit or withdrawal
                if withdrawal > 0:
                    create_transfer_entry(
                        user_id=get_effective_user_id(),
                        date=row_date,
                        from_account_id=payment_account_id,
                        to_account_id=category_id,
                        amount=amount,
                        description=description,
                        batch_id=batch_id,
                    )
                else:
                    create_transfer_entry(
                        user_id=get_effective_user_id(),
                        date=row_date,
                        from_account_id=category_id,
                        to_account_id=payment_account_id,
                        amount=amount,
                        description=description,
                        batch_id=batch_id,
                    )
                imported += 1
            elif deposit > 0:
                create_cashbook_entry(
                    user_id=get_effective_user_id(),
                    date=row_date,
                    transaction_type="income",
                    payment_account_id=payment_account_id,
                    category_account_id=category_id,
                    amount=deposit,
                    description=description,
                    batch_id=batch_id,
                )
                imported += 1
            elif withdrawal > 0:
                create_cashbook_entry(
                    user_id=get_effective_user_id(),
                    date=row_date,
                    transaction_type="expense",
                    payment_account_id=payment_account_id,
                    category_account_id=category_id,
                    amount=withdrawal,
                    description=description,
                    batch_id=batch_id,
                )
                imported += 1
            else:
                skipped += 1

        delete_import_data(data_key)
        session.pop("web_data_key", None)
        session.pop("web_payment_account_id", None)

        flash(f"{imported}件を取り込みました。（スキップ: {skipped}件）", "success")
        return redirect(url_for("cashbook.index"))

    grouped_accounts = get_grouped_accounts(get_effective_user_id())
    return render_template(
        "web_import/confirm.html",
        parsed=parsed,
        payment_account=payment_account,
        default_expense_id=default_expense.id if default_expense else 0,
        default_income_id=default_income.id if default_income else 0,
        grouped_accounts=grouped_accounts,
    )
