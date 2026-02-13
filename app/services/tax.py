"""確定申告・年末調整の集計サービス"""

from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.medical import MedicalExpense


TAX_CATEGORY_LABELS = {
    "social_insurance": "社会保険料控除",
    "life_insurance": "生命保険料控除",
    "earthquake_insurance": "地震保険料控除",
    "medical": "医療費控除",
    "donation": "寄附金控除",
    "ideco": "小規模企業共済等掛金控除",
    "withholding_tax": "源泉所得税",
    "resident_tax": "住民税",
}


def get_tax_summary(user_id, year):
    """確定申告用の年間控除額集計"""
    start = date(year, 1, 1)
    end = date(year, 12, 31)

    results = (
        db.session.query(
            Account.tax_category,
            Account.name,
            func.sum(JournalEntryLine.debit_amount).label("total"),
        )
        .join(JournalEntryLine, JournalEntryLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.tax_category.isnot(None),
            JournalEntry.date >= start,
            JournalEntry.date <= end,
        )
        .group_by(Account.tax_category, Account.name)
        .order_by(Account.tax_category, Account.name)
        .all()
    )

    summary = {}
    for tax_cat, account_name, total in results:
        if tax_cat not in summary:
            summary[tax_cat] = {
                "label": TAX_CATEGORY_LABELS.get(tax_cat, tax_cat),
                "accounts": [],
                "total": Decimal(0),
            }
        amount = total or Decimal(0)
        summary[tax_cat]["accounts"].append({"name": account_name, "amount": amount})
        summary[tax_cat]["total"] += amount

    return summary


def get_medical_summary(user_id, year):
    """医療費控除用の年間集計"""
    start = date(year, 1, 1)
    end = date(year, 12, 31)

    expenses = (
        MedicalExpense.query
        .filter(
            MedicalExpense.user_id == user_id,
            MedicalExpense.date >= start,
            MedicalExpense.date <= end,
        )
        .order_by(MedicalExpense.date)
        .all()
    )

    total_paid = sum(e.amount_paid for e in expenses)
    total_reimbursed = sum(e.insurance_reimbursement for e in expenses)
    net_total = total_paid - total_reimbursed

    return {
        "expenses": expenses,
        "total_paid": total_paid,
        "total_reimbursed": total_reimbursed,
        "net_total": net_total,
        "deductible": max(0, net_total - 100000),
    }


def get_income_expense_summary(user_id, year, month=None):
    """収支サマリー（月次 or 年次）"""
    start = date(year, month or 1, 1)
    if month:
        if month == 12:
            end = date(year + 1, 1, 1)
        else:
            end = date(year, month + 1, 1)
    else:
        end = date(year + 1, 1, 1)

    from app.models.account import AccountType

    revenue_type = AccountType.query.filter_by(code="revenue").first()
    expense_type = AccountType.query.filter_by(code="expense").first()

    if not revenue_type or not expense_type:
        return {"income": Decimal(0), "expense": Decimal(0), "balance": Decimal(0)}

    income = (
        db.session.query(func.coalesce(func.sum(JournalEntryLine.credit_amount), 0))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .filter(
            JournalEntry.user_id == user_id,
            JournalEntry.date >= start,
            JournalEntry.date < end,
            Account.account_type_id == revenue_type.id,
        )
        .scalar()
    )

    expense = (
        db.session.query(func.coalesce(func.sum(JournalEntryLine.debit_amount), 0))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .filter(
            JournalEntry.user_id == user_id,
            JournalEntry.date >= start,
            JournalEntry.date < end,
            Account.account_type_id == expense_type.id,
        )
        .scalar()
    )

    return {
        "income": income,
        "expense": expense,
        "balance": income - expense,
    }
