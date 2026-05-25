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
            Account.code,
            (func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
             - func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)).label("total"),
        )
        .join(JournalEntryLine, db.and_(JournalEntryLine.account_user_id == Account.user_id, JournalEntryLine.account_code == Account.code))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.tax_category.isnot(None),
            Account.tax_category.notin_(["medical", "resident_tax"]),
            JournalEntry.date >= start,
            JournalEntry.date <= end,
            JournalEntry.source != "closing",
        )
        .group_by(Account.tax_category, Account.name, Account.code)
        .order_by(Account.tax_category, Account.name)
        .all()
    )

    summary = {}
    for tax_cat, account_name, account_code, total in results:
        if tax_cat not in summary:
            summary[tax_cat] = {
                "label": TAX_CATEGORY_LABELS.get(tax_cat, tax_cat),
                "accounts": [],
                "total": Decimal(0),
            }
        amount = total or Decimal(0)
        summary[tax_cat]["accounts"].append(
            {"name": account_name, "code": account_code, "amount": amount}
        )
        summary[tax_cat]["total"] += amount

    return summary


def get_medical_summary(user_id, year):
    """医療費控除用の年間集計（仕訳から自動集計）"""
    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)

    # 医療費科目（tax_category="medical"）への借方仕訳を取得
    rows = (
        db.session.query(
            JournalEntry.id.label("entry_id"),
            JournalEntry.date,
            JournalEntry.description,
            JournalEntryLine.debit_amount,
            Account.name.label("account_name"),
        )
        .join(JournalEntryLine, db.and_(JournalEntryLine.account_user_id == Account.user_id, JournalEntryLine.account_code == Account.code))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.tax_category == "medical",
            JournalEntryLine.debit_amount > 0,
            JournalEntry.date >= start,
            JournalEntry.date < end,
        )
        .order_by(JournalEntry.date)
        .all()
    )

    # 紐付く MedicalExpense レコードを取得
    entry_ids = [r.entry_id for r in rows]
    medical_details = {}
    if entry_ids:
        me_records = (
            MedicalExpense.query
            .filter(MedicalExpense.journal_entry_id.in_(entry_ids))
            .all()
        )
        for me in me_records:
            medical_details[me.journal_entry_id] = me

    expenses = []
    for r in rows:
        me = medical_details.get(r.entry_id)
        expenses.append({
            "date": r.date,
            "description": r.description,
            "amount": int(r.debit_amount),
            "account_name": r.account_name,
            "patient_name": me.patient_name if me else "",
            "hospital_name": me.hospital_name if me else "",
            "treatment_description": me.treatment_description if me else "",
            "provider_type": me.provider_type or "" if me else "",
            "insurance_reimbursement": me.insurance_reimbursement if me else 0,
        })

    total_paid = sum(e["amount"] for e in expenses)
    total_reimbursed = sum(e["insurance_reimbursement"] for e in expenses)
    net_total = total_paid - total_reimbursed

    # 受診者別 → 医療機関別の階層集計
    by_patient = {}
    for e in expenses:
        patient = e["patient_name"] or "(未設定)"
        hospital = e["hospital_name"] or "(未設定)"
        if patient not in by_patient:
            by_patient[patient] = {"hospitals": {}, "paid": 0, "reimbursed": 0}
        by_patient[patient]["paid"] += e["amount"]
        by_patient[patient]["reimbursed"] += e["insurance_reimbursement"]
        if hospital not in by_patient[patient]["hospitals"]:
            by_patient[patient]["hospitals"][hospital] = {
                "paid": 0, "reimbursed": 0, "provider_type": e["provider_type"],
            }
        by_patient[patient]["hospitals"][hospital]["paid"] += e["amount"]
        by_patient[patient]["hospitals"][hospital]["reimbursed"] += e["insurance_reimbursement"]

    by_patient_list = []
    for patient, pdata in sorted(by_patient.items(), key=lambda x: x[1]["paid"], reverse=True):
        hospitals = sorted(
            [{"name": h, "paid": v["paid"], "reimbursed": v["reimbursed"],
              "net": v["paid"] - v["reimbursed"], "provider_type": v["provider_type"]}
             for h, v in pdata["hospitals"].items()],
            key=lambda x: x["paid"], reverse=True,
        )
        by_patient_list.append({
            "name": patient,
            "paid": pdata["paid"],
            "reimbursed": pdata["reimbursed"],
            "net": pdata["paid"] - pdata["reimbursed"],
            "hospitals": hospitals,
        })

    return {
        "expenses": expenses,
        "total_paid": total_paid,
        "total_reimbursed": total_reimbursed,
        "net_total": net_total,
        "by_patient": by_patient_list,
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

    # 費用: 月別の正味発生額（借方 - 貸方）
    expense_rows = (
        db.session.query(
            Account.code,
            Account.name,
            Account.cost_type,
            func.extract("month", JournalEntry.date).label("m"),
            (func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
             - func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)).label("total"),
        )
        .join(JournalEntryLine, db.and_(JournalEntryLine.account_user_id == Account.user_id, JournalEntryLine.account_code == Account.code))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.account_type_id == expense_type.id,
            Account.is_active.is_(True),
            JournalEntry.date >= start,
            JournalEntry.date < end,
        )
        .group_by(Account.code, Account.name, Account.cost_type, "m")
        .all()
    )

    # 収益: 月別の正味発生額（貸方 - 借方）
    income_rows = (
        db.session.query(
            Account.code,
            Account.name,
            Account.cost_type,
            func.extract("month", JournalEntry.date).label("m"),
            (func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)
             - func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)).label("total"),
        )
        .join(JournalEntryLine, db.and_(JournalEntryLine.account_user_id == Account.user_id, JournalEntryLine.account_code == Account.code))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.account_type_id == revenue_type.id,
            Account.is_active.is_(True),
            JournalEntry.date >= start,
            JournalEntry.date < end,
        )
        .group_by(Account.code, Account.name, Account.cost_type, "m")
        .all()
    )

    def pivot(rows):
        accounts = {}
        for row in rows:
            code = row.code
            if code not in accounts:
                accounts[code] = {
                    "code": code, "name": row.name,
                    "cost_type": row.cost_type,
                    "months": [0] * 12, "total": 0,
                }
            m_idx = int(row.m) - 1
            amount = int(row.total)
            accounts[code]["months"][m_idx] = amount
            accounts[code]["total"] += amount
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


def _get_daily_amounts_28d(user_id, reference_date):
    """過去28日間の variable 科目の日別発生額を返す。

    Returns:
        dict: {account_code: {date: amount, ...}, ...}
    """
    from datetime import timedelta
    from app.models.account import AccountType

    start_date = reference_date - timedelta(days=27)

    revenue_type = AccountType.query.filter_by(code="revenue").first()
    expense_type = AccountType.query.filter_by(code="expense").first()
    if not revenue_type or not expense_type:
        return {}

    # 費用: debit - credit
    expense_rows = (
        db.session.query(
            Account.code,
            JournalEntry.date,
            (func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
             - func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)).label("amount"),
        )
        .join(JournalEntryLine, db.and_(
            JournalEntryLine.account_user_id == Account.user_id,
            JournalEntryLine.account_code == Account.code,
        ))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.account_type_id == expense_type.id,
            Account.is_active.is_(True),
            Account.cost_type == "variable",
            JournalEntry.date >= start_date,
            JournalEntry.date <= reference_date,
            JournalEntry.source != "closing",
        )
        .group_by(Account.code, JournalEntry.date)
        .all()
    )

    # 収益: credit - debit
    income_rows = (
        db.session.query(
            Account.code,
            JournalEntry.date,
            (func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)
             - func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)).label("amount"),
        )
        .join(JournalEntryLine, db.and_(
            JournalEntryLine.account_user_id == Account.user_id,
            JournalEntryLine.account_code == Account.code,
        ))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.account_type_id == revenue_type.id,
            Account.is_active.is_(True),
            Account.cost_type == "variable",
            JournalEntry.date >= start_date,
            JournalEntry.date <= reference_date,
            JournalEntry.source != "closing",
        )
        .group_by(Account.code, JournalEntry.date)
        .all()
    )

    result = {}
    for row in expense_rows + income_rows:
        if row.code not in result:
            result[row.code] = {}
        result[row.code][row.date] = int(row.amount)
    return result


def _project_rolling28(actual, daily_data, today, days_in_month, days_elapsed):
    """rolling28: 過去28日間の日平均 × 残り日数 + 今月実績"""
    if not daily_data:
        return int(actual * days_in_month / days_elapsed) if days_elapsed > 0 else 0
    total_28d = sum(daily_data.values())
    daily_avg = total_28d / 28
    remaining_days = days_in_month - days_elapsed
    return actual + int(daily_avg * remaining_days)


def _project_dow28(actual, daily_data, today, days_in_month, days_elapsed):
    """dow28: 過去28日間の曜日別平均 × 残り各日の曜日 + 今月実績"""
    from datetime import timedelta

    if not daily_data:
        return int(actual * days_in_month / days_elapsed) if days_elapsed > 0 else 0

    # 曜日別集計（0=月, 6=日）— 28日 = 4週なので各曜日4回
    dow_totals = [0] * 7
    yesterday = today - timedelta(days=1)
    for i in range(28):
        d = yesterday - timedelta(days=i)
        dow_totals[d.weekday()] += daily_data.get(d, 0)
    dow_avg = [t / 4 for t in dow_totals]

    # 残り日数の各曜日平均を合計
    remaining_sum = 0
    for day_offset in range(1, days_in_month - days_elapsed + 1):
        future_date = today + timedelta(days=day_offset)
        remaining_sum += dow_avg[future_date.weekday()]

    return actual + int(remaining_sum)


def get_month_projection(user_id, year, month, comparison, method="pro_rata"):
    """当月の着地予想を計算"""
    import calendar
    from datetime import timedelta

    today = date.today()
    days_elapsed = today.day
    days_in_month = calendar.monthrange(year, month)[1]
    m_idx = month - 1

    prev_idx = m_idx - 1  # 前月インデックス（1月なら-1=前年12月はデータなし→0扱い）

    # rolling28 / dow28 用: 過去28日間データ（必要な場合のみ取得）
    daily_amounts = {}
    if method in ("rolling28", "dow28") and days_elapsed > 0:
        yesterday = today - timedelta(days=1)
        daily_amounts = _get_daily_amounts_28d(user_id, yesterday)

    def project(accounts):
        result = []
        for a in accounts:
            actual = a["months"][m_idx]
            ct = a["cost_type"] or "occasional"
            if ct == "fixed":
                # 固定: 前月発生額を予測値とする
                prev = a["months"][prev_idx] if prev_idx >= 0 else 0
                projected = prev if prev > 0 else actual
            elif ct == "variable":
                if method == "rolling28" and days_elapsed > 0:
                    projected = _project_rolling28(
                        actual, daily_amounts.get(a["code"], {}),
                        today, days_in_month, days_elapsed,
                    )
                elif method == "dow28" and days_elapsed > 0:
                    projected = _project_dow28(
                        actual, daily_amounts.get(a["code"], {}),
                        today, days_in_month, days_elapsed,
                    )
                else:
                    # 変動: 経過日数で按分して月末着地を予測
                    projected = int(actual * days_in_month / days_elapsed) if days_elapsed > 0 else 0
            else:
                # 随時: 実績をそのまま採用
                projected = actual
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
        "method": method,
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
        db.session.query(
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)
            - func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Account, db.and_(Account.user_id == JournalEntryLine.account_user_id, Account.code == JournalEntryLine.account_code))
        .filter(
            JournalEntry.user_id == user_id,
            JournalEntry.date >= start,
            JournalEntry.date < end,
            Account.account_type_id == revenue_type.id,
        )
        .scalar()
    )

    expense = (
        db.session.query(
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
            - func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Account, db.and_(Account.user_id == JournalEntryLine.account_user_id, Account.code == JournalEntryLine.account_code))
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
