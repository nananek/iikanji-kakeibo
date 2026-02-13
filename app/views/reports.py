from datetime import date
from decimal import Decimal

from flask import Blueprint, render_template, request
from flask_login import login_required, current_user
from sqlalchemy import func

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.tax import get_tax_summary, get_medical_summary, get_income_expense_summary

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

    balances = []
    for account in accounts:
        result = (
            db.session.query(
                func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
            )
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .filter(
                JournalEntryLine.account_id == account.id,
                JournalEntry.date <= as_of,
            )
            .first()
        )
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
