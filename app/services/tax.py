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


def get_monthly_comparison(user_id, year):
    """年間の科目別月次比較データを返す"""
    import calendar
    from app.models.account import AccountType

    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)

    revenue_type = AccountType.query.filter_by(code="revenue").first()
    expense_type = AccountType.query.filter_by(code="expense").first()

    if not revenue_type or not expense_type:
        return {
            "expense_accounts": [], "income_accounts": [],
            "expense_totals": [0] * 12, "income_totals": [0] * 12,
        }

    # 費用: 月別の借方合計
    expense_rows = (
        db.session.query(
            Account.id,
            Account.code,
            Account.name,
            Account.cost_type,
            func.extract("month", JournalEntry.date).label("m"),
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0).label("total"),
        )
        .join(JournalEntryLine, JournalEntryLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.account_type_id == expense_type.id,
            Account.is_active.is_(True),
            JournalEntry.date >= start,
            JournalEntry.date < end,
        )
        .group_by(Account.id, Account.code, Account.name, Account.cost_type, "m")
        .all()
    )

    # 収益: 月別の貸方合計
    income_rows = (
        db.session.query(
            Account.id,
            Account.code,
            Account.name,
            Account.cost_type,
            func.extract("month", JournalEntry.date).label("m"),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0).label("total"),
        )
        .join(JournalEntryLine, JournalEntryLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.account_type_id == revenue_type.id,
            Account.is_active.is_(True),
            JournalEntry.date >= start,
            JournalEntry.date < end,
        )
        .group_by(Account.id, Account.code, Account.name, Account.cost_type, "m")
        .all()
    )

    def pivot(rows):
        accounts = {}
        for row in rows:
            aid = row.id
            if aid not in accounts:
                accounts[aid] = {
                    "id": aid, "code": row.code, "name": row.name,
                    "cost_type": row.cost_type,
                    "months": [0] * 12, "total": 0,
                }
            m_idx = int(row.m) - 1
            amount = int(row.total)
            accounts[aid]["months"][m_idx] = amount
            accounts[aid]["total"] += amount
        return sorted(accounts.values(), key=lambda a: a["code"])

    expense_accounts = pivot(expense_rows)
    income_accounts = pivot(income_rows)

    expense_totals = [0] * 12
    for a in expense_accounts:
        for i in range(12):
            expense_totals[i] += a["months"][i]

    income_totals = [0] * 12
    for a in income_accounts:
        for i in range(12):
            income_totals[i] += a["months"][i]

    return {
        "expense_accounts": expense_accounts,
        "income_accounts": income_accounts,
        "expense_totals": expense_totals,
        "income_totals": income_totals,
    }


def get_month_projection(user_id, year, month, comparison):
    """当月の着地予想を計算"""
    import calendar

    today = date.today()
    days_elapsed = today.day
    days_in_month = calendar.monthrange(year, month)[1]
    m_idx = month - 1

    def project(accounts):
        result = []
        for a in accounts:
            actual = a["months"][m_idx]
            if a["cost_type"] in ("fixed", "occasional"):
                projected = actual
            elif days_elapsed > 0:
                projected = int(actual * days_in_month / days_elapsed)
            else:
                projected = 0
            result.append({
                "name": a["name"],
                "cost_type": a["cost_type"],
                "actual": actual,
                "projected": projected,
            })
        return result

    expense_projected = project(comparison["expense_accounts"])
    income_projected = project(comparison["income_accounts"])

    return {
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "month": month,
        "expense_projected": expense_projected,
        "income_projected": income_projected,
        "expense_total_actual": comparison["expense_totals"][m_idx],
        "expense_total_projected": sum(p["projected"] for p in expense_projected),
        "income_total_actual": comparison["income_totals"][m_idx],
        "income_total_projected": sum(p["projected"] for p in income_projected),
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
