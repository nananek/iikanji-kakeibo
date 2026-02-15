"""月次確定・決算期間管理サービス"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.user import User
from app.services.accounting import get_next_entry_number


# 期間番号 → 表示名
PERIOD_LABELS = {
    0: "期首振戻月",
    1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月",
    7: "7月", 8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月",
    13: "決算月1", 14: "決算月2", 15: "決算月3",
    16: "損益振替",
}


def adjust_date_for_fiscal_period(entry_date, fiscal_period):
    """特殊期間に応じて日付を補正する。期首振戻→1/1、決算月→12/31"""
    if fiscal_period is None:
        return entry_date
    year = entry_date.year
    if fiscal_period == 0:
        return date(year, 1, 1)
    if fiscal_period in (13, 14, 15, 16):
        return date(year, 12, 31)
    return entry_date


def get_effective_period(entry):
    """仕訳の実効期間を返す"""
    if entry.fiscal_period is not None:
        return entry.fiscal_period
    return entry.date.month


def get_closed_period(user_id, year):
    """指定年度の確定済み期間を返す（-1 = 未確定）"""
    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    return fc.closed_period if fc else -1


def get_closed_periods_for_dates(user_id, dates):
    """日付リストに含まれる年度の確定済み期間を辞書で返す {year: closed_period}"""
    years = set()
    for d in dates:
        if d:
            try:
                years.add(int(d[:4]))
            except (ValueError, TypeError):
                pass
    result = {}
    for y in years:
        cp = get_closed_period(user_id, y)
        if cp >= 0:
            result[y] = cp
    return result


def is_period_locked(user_id, year, period):
    """指定期間がロック済みか判定"""
    return period <= get_closed_period(user_id, year)


def check_entry_modifiable(user_id, entry):
    """仕訳が変更可能か判定。不可ならエラーメッセージを返す"""
    if entry.source == "closing":
        return "損益振替仕訳（自動生成）は変更できません。"
    year = entry.date.year
    period = get_effective_period(entry)
    if is_period_locked(user_id, year, period):
        label = PERIOD_LABELS.get(period, f"{period}月")
        return f"{year}年{label}は確定済みのため変更できません。"
    return None


def check_period_open_for_new(user_id, year, period):
    """新規仕訳の対象期間がオープンか判定"""
    if not is_year_open(user_id, year):
        return f"{year}年度は開設されていません。月次確定画面で年度を追加してください。"
    if is_period_locked(user_id, year, period):
        label = PERIOD_LABELS.get(period, f"{period}月")
        return f"{year}年{label}は確定済みのため仕訳を追加できません。"
    return None


def is_year_open(user_id, year):
    """年度が仕訳入力可能か判定。前年以降は常にTrue、前々年以前はFiscalCloseレコード要"""
    user = User.query.get(user_id)
    if not user:
        return False
    created_year = user.created_at.year
    if year >= created_year - 1:
        return True
    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    return fc is not None


def get_restricted_before_year(user_id):
    """制限対象となる年度の境界を返す（この年より前が制限対象）"""
    user = User.query.get(user_id)
    if not user:
        return None
    return user.created_at.year - 1


def get_capital_account_id(user_id):
    """元入金科目のIDを返す"""
    account = Account.query.filter_by(
        user_id=user_id, system_role="capital"
    ).first()
    return account.id if account else None


def close_period(user_id, year, period):
    """月次確定を実行。成功時はNone、エラー時はメッセージを返す"""
    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    current = fc.closed_period if fc else -1

    if period <= current:
        return "この期間は既に確定済みです。"
    if period != current + 1:
        prev_label = PERIOD_LABELS.get(current + 1, f"{current + 1}月")
        return f"先に{prev_label}を確定してください。"

    if not fc:
        fc = FiscalClose(user_id=user_id, year=year, closed_period=period)
        db.session.add(fc)
    else:
        fc.closed_period = period

    # 決算月3確定 → 損益振替仕訳を自動生成
    if period == 15:
        err = generate_closing_entries(user_id, year)
        if err:
            db.session.rollback()
            return err

    db.session.commit()
    return None


def reopen_period(user_id, year, period):
    """月次確定を解除。成功時はNone、エラー時はメッセージを返す"""
    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    if not fc or fc.closed_period < period:
        return "この期間は確定されていません。"
    if fc.closed_period != period:
        return "最後に確定した期間のみ解除できます。"

    # 決算月3解除 → 損益振替仕訳を削除
    if period == 15:
        delete_closing_entries(user_id, year)

    fc.closed_period = period - 1
    db.session.commit()
    return None


def generate_closing_entries(user_id, year):
    """損益振替仕訳を生成"""
    revenue_type = AccountType.query.filter_by(code="revenue").first()
    expense_type = AccountType.query.filter_by(code="expense").first()
    retained = Account.query.filter_by(user_id=user_id, system_role="retained_earnings").first()

    if not revenue_type or not expense_type or not retained:
        return "勘定科目（収益・費用・繰越利益）が見つかりません。"

    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    batch = f"closing-{year}-{uuid.uuid4().hex[:8]}"
    closing_date = date(year, 12, 31)

    # 収益科目ごとの貸方合計（= 収益残高）
    revenue_balances = (
        db.session.query(
            Account.id,
            Account.name,
            (func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)
             - func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)).label("balance"),
        )
        .join(JournalEntryLine, JournalEntryLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.account_type_id == revenue_type.id,
            JournalEntry.date >= start,
            JournalEntry.date < end,
        )
        .group_by(Account.id, Account.name)
        .having(
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)
            - func.coalesce(func.sum(JournalEntryLine.debit_amount), 0) != 0
        )
        .all()
    )

    # 費用科目ごとの借方合計（= 費用残高）
    expense_balances = (
        db.session.query(
            Account.id,
            Account.name,
            (func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
             - func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)).label("balance"),
        )
        .join(JournalEntryLine, JournalEntryLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.account_type_id == expense_type.id,
            JournalEntry.date >= start,
            JournalEntry.date < end,
        )
        .group_by(Account.id, Account.name)
        .having(
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
            - func.coalesce(func.sum(JournalEntryLine.credit_amount), 0) != 0
        )
        .all()
    )

    if not revenue_balances and not expense_balances:
        return None  # 振替不要

    lines_data = []

    # 収益振替: 借方=収益科目（ゼロに）/ 貸方=繰越利益
    total_revenue = Decimal(0)
    for acct_id, acct_name, balance in revenue_balances:
        amt = int(balance)
        if amt > 0:
            lines_data.append({
                "account_id": acct_id, "debit_amount": amt, "credit_amount": 0,
            })
            total_revenue += amt
        elif amt < 0:
            lines_data.append({
                "account_id": acct_id, "debit_amount": 0, "credit_amount": -amt,
            })
            total_revenue += amt

    # 費用振替: 貸方=費用科目（ゼロに）/ 借方=繰越利益
    total_expense = Decimal(0)
    for acct_id, acct_name, balance in expense_balances:
        amt = int(balance)
        if amt > 0:
            lines_data.append({
                "account_id": acct_id, "debit_amount": 0, "credit_amount": amt,
            })
            total_expense += amt
        elif amt < 0:
            lines_data.append({
                "account_id": acct_id, "debit_amount": -amt, "credit_amount": 0,
            })
            total_expense += amt

    # 繰越利益への振替
    net = int(total_revenue - total_expense)
    if net > 0:
        lines_data.append({
            "account_id": retained.id, "debit_amount": 0, "credit_amount": net,
        })
    elif net < 0:
        lines_data.append({
            "account_id": retained.id, "debit_amount": -net, "credit_amount": 0,
        })

    if not lines_data:
        return None

    entry = JournalEntry(
        user_id=user_id,
        date=closing_date,
        entry_number=get_next_entry_number(user_id),
        description="損益振替仕訳（自動生成）",
        source="closing",
        batch_id=batch,
        fiscal_period=16,
    )
    db.session.add(entry)
    db.session.flush()

    for ld in lines_data:
        db.session.add(JournalEntryLine(
            journal_entry_id=entry.id,
            account_id=ld["account_id"],
            debit_amount=ld["debit_amount"],
            credit_amount=ld["credit_amount"],
        ))

    return None


def delete_closing_entries(user_id, year):
    """自動生成した損益振替仕訳を削除"""
    entries = JournalEntry.query.filter(
        JournalEntry.user_id == user_id,
        JournalEntry.source == "closing",
        JournalEntry.fiscal_period == 16,
        JournalEntry.date >= date(year, 1, 1),
        JournalEntry.date < date(year + 1, 1, 1),
    ).all()
    for entry in entries:
        db.session.delete(entry)
