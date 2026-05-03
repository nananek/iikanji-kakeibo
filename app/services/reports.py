"""レポート系の共通計算ロジック"""

from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.balance_cache import get_cached_balances
from app.services.fiscal import get_closed_period, period_range_filter


def compute_trial_balance(user_id, year, pf=0, pt=15):
    """試算表データを計算する。

    Args:
        user_id: ユーザーID
        year: 対象年度
        pf, pt: 期間範囲 (0=期首, 1-12=月, 13-15=決算整理, 16=損益振替)

    Returns:
        list of dict: 各科目の {account_code, account_name, account_type,
        normal_balance, opening, debit, credit, balance}
    """
    pf = max(0, min(16, pf))
    pt = max(pf, min(16, pt))
    incl_closing = pt >= 16

    accounts = (
        Account.query.filter_by(user_id=user_id).order_by(Account.code).all()
    )
    accounts = [
        a for a in accounts
        if a.is_active or (a.deactivated_year and a.deactivated_year >= year)
    ]

    start_of_year = date(year, 1, 1)
    pl_codes = {"revenue", "expense"}
    bs_codes = {"asset", "liability", "equity"}

    closed = get_closed_period(user_id, year)
    cache = {}
    use_cache = pf > 0 and closed >= pf - 1
    if use_cache:
        cache = get_cached_balances(user_id, year, pf - 1)

    current_filter = period_range_filter(year, pf, pt)

    def _query_sum(code, filters, include_closing=False):
        q = (
            db.session.query(
                func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
            )
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .filter(
                JournalEntryLine.account_user_id == user_id,
                JournalEntryLine.account_code == code,
            )
        )
        if not include_closing:
            q = q.filter(JournalEntry.source != "closing")
        for f in filters:
            q = q.filter(f)
        return q.first()

    result = []
    for account in accounts:
        type_code = account.account_type.code
        is_pl = type_code in pl_codes
        is_bs = type_code in bs_codes
        is_debit_normal = account.account_type.normal_balance == "debit"

        debit, credit = _query_sum(
            account.code, [current_filter], include_closing=incl_closing
        )
        period_debit = int(debit)
        period_credit = int(credit)

        opening = 0
        if pf > 0 and use_cache and account.code in cache:
            year_d, year_c = cache[account.code]
            if is_bs:
                bd, bc = _query_sum(
                    account.code,
                    [JournalEntry.date < start_of_year],
                    include_closing=True,
                )
                bd, bc = int(bd), int(bc)
                if is_debit_normal:
                    opening = (year_d + bd) - (year_c + bc)
                else:
                    opening = (year_c + bc) - (year_d + bd)
            else:
                if is_debit_normal:
                    opening = year_d - year_c
                else:
                    opening = year_c - year_d
        elif pf > 0:
            # pf >= 1 なので period_range_filter は必ず非 None
            prior_filter = period_range_filter(year, 0, pf - 1)
            if is_bs:
                pd, pc = _query_sum(account.code, [prior_filter])
                pd, pc = int(pd), int(pc)
                bd, bc = _query_sum(
                    account.code,
                    [JournalEntry.date < start_of_year],
                    include_closing=True,
                )
                bd, bc = int(bd), int(bc)
                if is_debit_normal:
                    opening = (pd + bd) - (pc + bc)
                else:
                    opening = (pc + bc) - (pd + bd)
            elif is_pl:
                pd, pc = _query_sum(account.code, [prior_filter])
                if is_debit_normal:
                    opening = int(pd) - int(pc)
                else:
                    opening = int(pc) - int(pd)
        elif is_bs:
            bd, bc = _query_sum(
                account.code,
                [JournalEntry.date < start_of_year],
                include_closing=True,
            )
            bd, bc = int(bd), int(bc)
            if is_debit_normal:
                opening = bd - bc
            else:
                opening = bc - bd

        if is_debit_normal:
            balance = opening + period_debit - period_credit
        else:
            balance = opening + period_credit - period_debit

        if period_debit != 0 or period_credit != 0 or opening != 0:
            result.append({
                "account_code": account.code,
                "account_name": account.name,
                "account_type": type_code,
                "normal_balance": account.account_type.normal_balance,
                "opening": opening,
                "debit": period_debit,
                "credit": period_credit,
                "balance": balance,
            })

    return result


def compute_income_statement(user_id, year, month=None):
    """損益計算書データ (科目別内訳付き)

    Args:
        user_id: ユーザーID
        year: 対象年度
        month: 対象月 (1-12)。None なら年間。

    Returns:
        dict: {income_total, expense_total, net_income, income_breakdown[],
        expense_breakdown[]}
    """
    from app.models.account import AccountType

    start = date(year, month or 1, 1)
    if month:
        end = date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)
    else:
        end = date(year + 1, 1, 1)

    revenue_type = AccountType.query.filter_by(code="revenue").first()
    expense_type = AccountType.query.filter_by(code="expense").first()
    if not revenue_type or not expense_type:
        return {
            "income_total": 0, "expense_total": 0, "net_income": 0,
            "income_breakdown": [], "expense_breakdown": [],
        }

    def _breakdown(account_type_id, sign_credit_minus_debit):
        """科目別の集計を返す (収益は credit-debit、費用は debit-credit)"""
        rows = (
            db.session.query(
                Account.code,
                Account.name,
                (
                    func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)
                    - func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
                ).label("net") if sign_credit_minus_debit else (
                    func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
                    - func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)
                ).label("net"),
            )
            .join(
                JournalEntryLine,
                db.and_(
                    JournalEntryLine.account_user_id == Account.user_id,
                    JournalEntryLine.account_code == Account.code,
                ),
            )
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .filter(
                Account.user_id == user_id,
                Account.account_type_id == account_type_id,
                JournalEntry.date >= start,
                JournalEntry.date < end,
                JournalEntry.source != "closing",
            )
            .group_by(Account.code, Account.name)
            .order_by(Account.code)
            .all()
        )
        return [
            {"account_code": r.code, "account_name": r.name, "amount": int(r.net)}
            for r in rows if int(r.net) != 0
        ]

    income_breakdown = _breakdown(revenue_type.id, sign_credit_minus_debit=True)
    expense_breakdown = _breakdown(expense_type.id, sign_credit_minus_debit=False)

    income_total = sum(item["amount"] for item in income_breakdown)
    expense_total = sum(item["amount"] for item in expense_breakdown)
    return {
        "income_total": income_total,
        "expense_total": expense_total,
        "net_income": income_total - expense_total,
        "income_breakdown": income_breakdown,
        "expense_breakdown": expense_breakdown,
    }
