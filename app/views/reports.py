import csv
import io
from datetime import date, timedelta
from decimal import Decimal

from flask import Blueprint, render_template, request, Response
from flask_login import login_required, current_user
from sqlalchemy import func, and_

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.tax import (
    get_tax_summary, get_medical_summary, get_income_expense_summary,
    get_monthly_comparison, get_month_projection,
)
from app.views.helpers import get_grouped_accounts

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/")
@login_required
def index():
    return render_template("reports/index.html")


@bp.route("/balance")
@login_required
def balance():
    """残高試算表"""
    year = request.args.get("year", date.today().year, type=int)
    as_of = date(year, 12, 31)

    account_types = AccountType.query.order_by(AccountType.display_order).all()
    accounts = (
        Account.query
        .filter_by(user_id=current_user.id, is_active=True)
        .order_by(Account.code)
        .all()
    )

    # P/L科目は当年発生額、B/S科目は累計残高
    start_of_year = date(year, 1, 1)
    pl_type_codes = {"revenue", "expense"}

    balances = []
    for account in accounts:
        is_pl = account.account_type.code in pl_type_codes

        query = (
            db.session.query(
                func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
            )
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .filter(
                JournalEntryLine.account_id == account.id,
                JournalEntry.date <= as_of,
            )
        )

        if is_pl:
            query = query.filter(JournalEntry.date >= start_of_year)

        result = query.first()
        total_debit = result[0]
        total_credit = result[1]

        if account.account_type.normal_balance == "debit":
            balance_amount = total_debit - total_credit
        else:
            balance_amount = total_credit - total_debit

        if total_debit != 0 or total_credit != 0:
            balances.append({
                "account": account,
                "debit": total_debit,
                "credit": total_credit,
                "balance": balance_amount,
            })

    return render_template(
        "reports/balance.html",
        year=year,
        balances=balances,
        account_types=account_types,
    )


@bp.route("/pl")
@login_required
def pl():
    """収支計算書"""
    year = request.args.get("year", date.today().year, type=int)
    month = request.args.get("month", 0, type=int)

    if month:
        summary = get_income_expense_summary(current_user.id, year, month)
    else:
        summary = get_income_expense_summary(current_user.id, year)

    # 科目別内訳
    revenue_type = AccountType.query.filter_by(code="revenue").first()
    expense_type = AccountType.query.filter_by(code="expense").first()

    start = date(year, month or 1, 1)
    if month:
        if month == 12:
            end = date(year + 1, 1, 1)
        else:
            end = date(year, month + 1, 1)
    else:
        end = date(year + 1, 1, 1)

    def get_breakdown(type_id, amount_col):
        return (
            db.session.query(
                Account.name,
                func.coalesce(func.sum(amount_col), 0).label("total"),
            )
            .join(JournalEntryLine, JournalEntryLine.account_id == Account.id)
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .filter(
                Account.user_id == current_user.id,
                Account.account_type_id == type_id,
                JournalEntry.date >= start,
                JournalEntry.date < end,
            )
            .group_by(Account.name, Account.code)
            .order_by(Account.code)
            .having(func.sum(amount_col) > 0)
            .all()
        )

    income_breakdown = (
        get_breakdown(revenue_type.id, JournalEntryLine.credit_amount)
        if revenue_type else []
    )
    expense_breakdown = (
        get_breakdown(expense_type.id, JournalEntryLine.debit_amount)
        if expense_type else []
    )

    return render_template(
        "reports/pl.html",
        year=year,
        month=month,
        summary=summary,
        income_breakdown=income_breakdown,
        expense_breakdown=expense_breakdown,
    )


@bp.route("/tax")
@login_required
def tax():
    """確定申告用集計"""
    year = request.args.get("year", date.today().year, type=int)

    tax_summary = get_tax_summary(current_user.id, year)
    medical_summary = get_medical_summary(current_user.id, year)

    return render_template(
        "reports/tax.html",
        year=year,
        tax_summary=tax_summary,
        medical_summary=medical_summary,
    )


@bp.route("/tax/medical-csv")
@login_required
def medical_csv():
    """医療費集計フォーム Ver 3.1 準拠CSVダウンロード"""
    year = request.args.get("year", date.today().year, type=int)
    medical_summary = get_medical_summary(current_user.id, year)

    output = io.StringIO()
    writer = csv.writer(output)

    # ヘッダー行（Ver 3.1 準拠: A〜H列）
    writer.writerow([
        "医療を受けた人",
        "病院・薬局などの名称",
        "診療・治療",
        "医薬品購入",
        "介護保険サービス",
        "その他の医療費",
        "支払った医療費の金額",
        "左のうち、補てんされる金額",
    ])

    for e in medical_summary["expenses"]:
        pt = e["provider_type"]
        writer.writerow([
            e["patient_name"],
            e["hospital_name"],
            "該当する" if pt == "hospital" or not pt else "",
            "該当する" if pt == "pharmacy" else "",
            "該当する" if pt == "nursing" else "",
            "該当する" if pt == "other" else "",
            e["amount"],
            e["insurance_reimbursement"] if e["insurance_reimbursement"] else "",
        ])

    csv_data = output.getvalue()
    output.close()

    # BOM付きUTF-8でExcel互換
    bom = "\ufeff"
    return Response(
        bom + csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="iryouhi_{year}.csv"',
        },
    )


@bp.route("/ledger")
@login_required
def ledger():
    """総勘定元帳"""
    year = request.args.get("year", date.today().year, type=int)
    month = request.args.get("month", 0, type=int)
    account_id = request.args.get("account_id", 0, type=int)

    account_types = AccountType.query.order_by(AccountType.display_order).all()
    accounts = (
        Account.query
        .filter_by(user_id=current_user.id, is_active=True)
        .order_by(Account.code)
        .all()
    )

    # 科目区分ごとにグルーピング
    grouped_accounts = {}
    for at in account_types:
        group = [a for a in accounts if a.account_type_id == at.id]
        if group:
            grouped_accounts[at] = group

    selected_account = None
    entries = []
    carry_forward = 0

    if account_id:
        selected_account = Account.query.filter_by(
            id=account_id, user_id=current_user.id
        ).first()

        if selected_account:
            # 表示期間の決定
            if month:
                period_start = date(year, month, 1)
                if month == 12:
                    period_end = date(year, 12, 31)
                else:
                    period_end = date(year, month + 1, 1) - timedelta(days=1)
            else:
                period_start = date(year, 1, 1)
                period_end = date(year, 12, 31)

            # 前期繰越（表示開始日より前の累計残高）
            cf_result = (
                db.session.query(
                    func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                    func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
                )
                .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
                .filter(
                    JournalEntryLine.account_id == account_id,
                    JournalEntry.date < period_start,
                )
                .first()
            )
            if selected_account.account_type.normal_balance == "debit":
                carry_forward = int(cf_result[0]) - int(cf_result[1])
            else:
                carry_forward = int(cf_result[1]) - int(cf_result[0])

            # 当期の仕訳明細を取得
            lines = (
                db.session.query(
                    JournalEntry.date,
                    JournalEntry.entry_number,
                    JournalEntry.description,
                    JournalEntryLine.debit_amount,
                    JournalEntryLine.credit_amount,
                    JournalEntryLine.journal_entry_id,
                    JournalEntry.id.label("entry_id"),
                )
                .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
                .filter(
                    JournalEntryLine.account_id == account_id,
                    JournalEntry.date >= period_start,
                    JournalEntry.date <= period_end,
                )
                .order_by(JournalEntry.date, JournalEntry.entry_number)
                .all()
            )

            running_balance = carry_forward
            for line in lines:
                debit = int(line.debit_amount)
                credit = int(line.credit_amount)
                if selected_account.account_type.normal_balance == "debit":
                    running_balance += debit - credit
                else:
                    running_balance += credit - debit

                # 相手科目を取得
                counter_lines = (
                    JournalEntryLine.query
                    .filter(
                        JournalEntryLine.journal_entry_id == line.journal_entry_id,
                        JournalEntryLine.account_id != account_id,
                    )
                    .all()
                )
                counter_names = ", ".join(
                    a.account.name for a in counter_lines
                ) if counter_lines else ""

                entries.append({
                    "date": line.date,
                    "entry_number": line.entry_number,
                    "description": line.description,
                    "counter_account": counter_names,
                    "debit": debit,
                    "credit": credit,
                    "balance": running_balance,
                    "entry_id": line.entry_id,
                })

    # モーダル用: 全科目データ
    all_grouped = get_grouped_accounts(current_user.id)

    return render_template(
        "reports/ledger.html",
        year=year,
        month=month,
        grouped_accounts=grouped_accounts,
        selected_account=selected_account,
        account_id=account_id,
        entries=entries,
        carry_forward=carry_forward,
        all_grouped_accounts=all_grouped,
    )


@bp.route("/monthly")
@login_required
def monthly():
    """月次比較レポート"""
    year = request.args.get("year", date.today().year, type=int)
    comparison = get_monthly_comparison(current_user.id, year)

    projection = None
    today = date.today()
    if year == today.year and today.day < \
            __import__("calendar").monthrange(year, today.month)[1]:
        projection = get_month_projection(
            current_user.id, year, today.month, comparison
        )

    return render_template(
        "reports/monthly.html",
        year=year,
        current_month=today.month if year == today.year else None,
        comparison=comparison,
        projection=projection,
    )
