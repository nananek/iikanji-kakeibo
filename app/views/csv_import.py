"""CSV明細取り込みビュー"""

import json

from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account, AccountType
from app.services.csv_import import (
    parse_csv_preview,
    parse_csv_full,
    DATE_FORMATS,
)
from app.services.accounting import create_cashbook_entry

bp = Blueprint("csv_import", __name__, url_prefix="/csv-import")

MAX_CSV_SIZE = 5 * 1024 * 1024  # 5MB


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


def _get_category_choices(user_id):
    expense_type = AccountType.query.filter_by(code="expense").first()
    revenue_type = AccountType.query.filter_by(code="revenue").first()
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
    return (
        [(a.id, f"【支出】{a.name}") for a in expenses]
        + [(a.id, f"【収入】{a.name}") for a in revenues]
    )


@bp.route("/", methods=["GET", "POST"])
@login_required
def upload():
    """Step 1: CSVアップロード"""
    payment_choices = _get_payment_choices(current_user.id)

    if request.method == "POST":
        csv_file = request.files.get("csv_file")
        if not csv_file or not csv_file.filename:
            flash("CSVファイルを選択してください。", "danger")
            return render_template(
                "csv_import/upload.html", payment_choices=payment_choices
            )

        raw_bytes = csv_file.read()
        if len(raw_bytes) > MAX_CSV_SIZE:
            flash("ファイルサイズが大きすぎます（上限5MB）。", "danger")
            return render_template(
                "csv_import/upload.html", payment_choices=payment_choices
            )

        payment_account_id = request.form.get("payment_account_id", type=int)
        if not payment_account_id:
            flash("取込先の口座を選択してください。", "danger")
            return render_template(
                "csv_import/upload.html", payment_choices=payment_choices
            )

        preview = parse_csv_preview(raw_bytes)
        if not preview["headers"] or not preview["rows"]:
            flash("CSVファイルの内容を読み取れませんでした。", "danger")
            return render_template(
                "csv_import/upload.html", payment_choices=payment_choices
            )

        # セッションにCSVデータと口座IDを保存
        import base64
        session["csv_raw"] = base64.b64encode(raw_bytes).decode("ascii")
        session["csv_payment_account_id"] = payment_account_id

        return redirect(url_for("csv_import.mapping"))

    return render_template(
        "csv_import/upload.html", payment_choices=payment_choices
    )


@bp.route("/mapping", methods=["GET", "POST"])
@login_required
def mapping():
    """Step 2: 列マッピング + プレビュー"""
    import base64

    csv_b64 = session.get("csv_raw")
    payment_account_id = session.get("csv_payment_account_id")
    if not csv_b64 or not payment_account_id:
        flash("CSVデータがありません。もう一度アップロードしてください。", "warning")
        return redirect(url_for("csv_import.upload"))

    raw_bytes = base64.b64decode(csv_b64)
    preview = parse_csv_preview(raw_bytes)
    headers = preview["headers"]
    col_indices = list(range(len(headers)))

    category_choices = _get_category_choices(current_user.id)
    payment_account = Account.query.get(payment_account_id)

    if request.method == "POST":
        date_col = request.form.get("date_col", type=int)
        desc_col = request.form.get("desc_col", type=int)
        date_format = request.form.get("date_format", "")
        amount_mode = request.form.get("amount_mode", "separate")

        mapping_data = {
            "date_col": date_col,
            "desc_col": desc_col,
        }

        if amount_mode == "single":
            amount_col = request.form.get("amount_col", type=int)
            mapping_data["amount_col"] = amount_col
            mapping_data["deposit_col"] = None
            mapping_data["withdrawal_col"] = None
        else:
            deposit_col = request.form.get("deposit_col", type=int)
            withdrawal_col = request.form.get("withdrawal_col", type=int)
            mapping_data["deposit_col"] = deposit_col
            mapping_data["withdrawal_col"] = withdrawal_col
            mapping_data["amount_col"] = None

        if date_col is None or desc_col is None:
            flash("日付列と摘要列は必須です。", "danger")
            return render_template(
                "csv_import/mapping.html",
                headers=headers,
                col_indices=col_indices,
                preview_rows=preview["rows"],
                total_rows=preview["total_rows"],
                date_formats=DATE_FORMATS,
                category_choices=category_choices,
                payment_account=payment_account,
            )

        # フルパース
        parsed = parse_csv_full(raw_bytes, mapping_data, date_format)

        if not parsed:
            flash("有効なデータ行が見つかりませんでした。マッピングを確認してください。", "danger")
            return render_template(
                "csv_import/mapping.html",
                headers=headers,
                col_indices=col_indices,
                preview_rows=preview["rows"],
                total_rows=preview["total_rows"],
                date_formats=DATE_FORMATS,
                category_choices=category_choices,
                payment_account=payment_account,
            )

        # パース結果をセッションに保存してconfirmへ
        serializable = []
        for p in parsed:
            serializable.append({
                "row_num": p["row_num"],
                "date": p["date"].isoformat() if p["date"] else None,
                "description": p["description"],
                "deposit": p["deposit"],
                "withdrawal": p["withdrawal"],
            })
        session["csv_parsed"] = json.dumps(serializable, ensure_ascii=False)

        return redirect(url_for("csv_import.confirm"))

    return render_template(
        "csv_import/mapping.html",
        headers=headers,
        col_indices=col_indices,
        preview_rows=preview["rows"],
        total_rows=preview["total_rows"],
        date_formats=DATE_FORMATS,
        category_choices=category_choices,
        payment_account=payment_account,
    )


@bp.route("/confirm", methods=["GET", "POST"])
@login_required
def confirm():
    """Step 3: 確認して一括取り込み"""
    import base64
    from datetime import date as date_type

    parsed_json = session.get("csv_parsed")
    payment_account_id = session.get("csv_payment_account_id")
    if not parsed_json or not payment_account_id:
        flash("データがありません。もう一度アップロードしてください。", "warning")
        return redirect(url_for("csv_import.upload"))

    parsed = json.loads(parsed_json)
    payment_account = Account.query.get(payment_account_id)
    category_choices = _get_category_choices(current_user.id)

    # デフォルト費目（食費=最初の支出科目）を取得
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

    if request.method == "POST":
        # 取り込み実行
        import_rows = request.form.get("import_rows", "")
        if not import_rows:
            flash("取り込むデータがありません。", "danger")
            return redirect(url_for("csv_import.upload"))

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

            if deposit > 0 and category_id:
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
            elif withdrawal > 0 and category_id:
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

        # セッションをクリア
        session.pop("csv_raw", None)
        session.pop("csv_payment_account_id", None)
        session.pop("csv_parsed", None)

        flash(f"{imported}件を取り込みました。（スキップ: {skipped}件）", "success")
        return redirect(url_for("cashbook.index"))

    return render_template(
        "csv_import/confirm.html",
        parsed=parsed,
        payment_account=payment_account,
        category_choices=category_choices,
        default_expense_id=default_expense.id if default_expense else 0,
        default_income_id=default_income.id if default_income else 0,
    )
