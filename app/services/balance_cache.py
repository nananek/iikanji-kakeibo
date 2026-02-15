"""確定済み期間の残高キャッシュ管理"""

from sqlalchemy import func

from app.extensions import db
from app.models.account import Account
from app.models.balance_cache import BalanceCache
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.fiscal import period_range_filter


def compute_balance_cache(user_id, year, period):
    """確定時: 全アカウントの当年累計 debit/credit をキャッシュする"""
    include_closing = period >= 16
    accounts = Account.query.filter_by(user_id=user_id).all()
    range_cond = period_range_filter(year, 0, period)

    for account in accounts:
        q = (
            db.session.query(
                func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
            )
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .filter(JournalEntryLine.account_id == account.id, range_cond)
        )
        if not include_closing:
            q = q.filter(JournalEntry.source != "closing")
        d, c = q.first()
        d, c = int(d), int(c)
        if d == 0 and c == 0:
            continue

        existing = BalanceCache.query.filter_by(
            user_id=user_id, account_id=account.id, year=year, period=period,
        ).first()
        if existing:
            existing.cumulative_debit = d
            existing.cumulative_credit = c
        else:
            db.session.add(BalanceCache(
                user_id=user_id, account_id=account.id,
                year=year, period=period,
                cumulative_debit=d, cumulative_credit=c,
            ))


def invalidate_balance_cache(user_id, year, from_period):
    """解除時: 該当期間以降のキャッシュを削除"""
    BalanceCache.query.filter(
        BalanceCache.user_id == user_id,
        BalanceCache.year == year,
        BalanceCache.period >= from_period,
    ).delete()


def get_cached_balances(user_id, year, period):
    """一括取得: {account_id: (debit, credit)}"""
    caches = BalanceCache.query.filter_by(
        user_id=user_id, year=year, period=period,
    ).all()
    return {
        c.account_id: (int(c.cumulative_debit), int(c.cumulative_credit))
        for c in caches
    }
