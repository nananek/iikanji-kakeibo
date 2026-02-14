"""OFX明細取り込みビュー"""

import json
import uuid
from datetime import date as date_type

from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account, AccountType
from app.services.ofx_import import parse_ofx
from app.services.accounting import create_cashbook_entry, create_transfer_entry
from app.services.fiscal import check_period_open_for_new
from app.views.helpers import (
    get_grouped_accounts, save_import_data, load_import_data, delete_import_data,
)

bp = Blueprint("ofx_import", __name__, url_prefix="/ofx-import")

MAX_OFX_SIZE = 5 * 1024 * 1024  # 5MB


@bp.route("/", methods=["GET", "POST"])
@login_required
def upload():
    """Step 1: OFXファイルアップロード"""
    grouped_accounts = get_grouped_accounts(current_user.id)

    if request.method == "POST":
        ofx_file = request.files.get("ofx_file")
        payment_account_id = request.form.get("payment_account_id", type=int)

        if not ofx_file or not ofx_file.filename:
            flash("OFXファイルを選択してください。", "danger")
            return render_template(
                "ofx_import/upload.html",
                grouped_accounts=grouped_accounts,
            )

        if not payment_account_id:
            flash("取込先の口座を選択してください。", "danger")
            return render_template(
                "ofx_import/upload.html",
                grouped_accounts=grouped_accounts,
            )

        file_bytes = ofx_file.read()
        if len(file_bytes) > MAX_OFX_SIZE:
            flash("ファイルサイズが大きすぎます（上限5MB）。", "danger")
            return render_template(
                "ofx_import/upload.html",
                grouped_accounts=grouped_accounts,
            )

        try:
            result = parse_ofx(file_bytes)
        except Exception as e:
            flash(f"OFXファイルの解析に失敗しました: {e}", "danger")
            return render_template(
                "ofx_import/upload.html",
                grouped_accounts=grouped_accounts,
            )

        if not result["rows"]:
            flash("取引データが見つかりませんでした。", "warning")
            return render_template(
                "ofx_import/upload.html",
                grouped_accounts=grouped_accounts,
            )

        # 一時ファイルに保存（Cookieサイズ制限を回避）
        key = save_import_data(result["rows"])
        session["ofx_data_key"] = key
        session["ofx_payment_account_id"] = payment_account_id
        session["ofx_account_info"] = result.get("account_id", "")

        flash(f"{len(result['rows'])}件の取引を検出しました。", "success")
        return redirect(url_for("ofx_import.confirm"))

    return render_template(
        "ofx_import/upload.html",
        grouped_accounts=grouped_accounts,
    )


@bp.route("/confirm", methods=["GET", "POST"])
@login_required
def confirm():
    """Step 2: 確認して一括取り込み"""
    data_key = session.get("ofx_data_key")
    payment_account_id = session.get("ofx_payment_account_id")
    parsed = load_import_data(data_key)

    if not parsed or not payment_account_id:
        flash("データがありません。もう一度アップロードしてください。", "warning")
        return redirect(url_for("ofx_import.upload"))

    payment_account = Account.query.get(payment_account_id)

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
            return redirect(url_for("ofx_import.upload"))

        rows_data = json.loads(import_rows)
        imported = 0
        skipped = 0
        batch_id = str(uuid.uuid4())

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

            # 確定済み期間チェック
            err = check_period_open_for_new(
                current_user.id, row_date.year, row_date.month
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
                        user_id=current_user.id,
                        date=row_date,
                        from_account_id=payment_account_id,
                        to_account_id=category_id,
                        amount=amount,
                        description=description,
                        batch_id=batch_id,
                    )
                else:
                    create_transfer_entry(
                        user_id=current_user.id,
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
                    user_id=current_user.id,
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
                    user_id=current_user.id,
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
        session.pop("ofx_data_key", None)
        session.pop("ofx_payment_account_id", None)
        session.pop("ofx_account_info", None)

        flash(f"{imported}件を取り込みました。（スキップ: {skipped}件）", "success")
        return redirect(url_for("cashbook.index"))

    grouped_accounts = get_grouped_accounts(current_user.id)
    return render_template(
        "ofx_import/confirm.html",
        parsed=parsed,
        payment_account=payment_account,
        default_expense_id=default_expense.id if default_expense else 0,
        default_income_id=default_income.id if default_income else 0,
        grouped_accounts=grouped_accounts,
        ofx_account_info=session.get("ofx_account_info", ""),
    )
