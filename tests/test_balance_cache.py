"""balance_cache サービスのテスト"""

from datetime import date

import pytest

from app.extensions import db
from app.models.balance_cache import BalanceCache
from app.services.balance_cache import (
    compute_balance_cache,
    get_cached_balances,
    invalidate_balance_cache,
)
from tests.conftest import make_journal


# ============================================================
# compute_balance_cache
# ============================================================

class TestComputeBalanceCache:
    def test_basic_cache(self, app, user, accounts):
        """基本的なキャッシュ計算"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)
        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

        balances = get_cached_balances(user.id, 2026, 1)
        # 食費（借方）
        assert balances[accounts["5010"].id] == (1000, 0)
        # 現金（貸方）
        assert balances[accounts["1010"].id] == (0, 1000)

    def test_cumulative_across_periods(self, app, user, accounts):
        """複数期間の累計が正しく計算される"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     2000, entry_date=date(2026, 2, 10), fiscal_period=2)

        compute_balance_cache(user.id, 2026, 2)
        db.session.commit()

        balances = get_cached_balances(user.id, 2026, 2)
        assert balances[accounts["5010"].id] == (3000, 0)
        assert balances[accounts["1010"].id] == (0, 3000)

    def test_zero_balance_not_cached(self, app, user, accounts):
        """残高ゼロの科目はキャッシュしない"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)
        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

        balances = get_cached_balances(user.id, 2026, 1)
        # 使用していない科目はキャッシュに含まれない
        assert accounts["1020"].id not in balances  # 普通預金

    def test_closing_excluded_for_normal_period(self, app, user, accounts):
        """通常期間では決算整理仕訳（closing）を除外"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)
        # closing 仕訳
        make_journal(db, user.id, accounts["1010"].id, accounts["5010"].id,
                     1000, entry_date=date(2026, 12, 31), source="closing",
                     fiscal_period=16)

        compute_balance_cache(user.id, 2026, 12)
        db.session.commit()

        balances = get_cached_balances(user.id, 2026, 12)
        # closing が除外されるので食費は1000のまま
        assert balances[accounts["5010"].id] == (1000, 0)

    def test_closing_included_for_period_16(self, app, user, accounts):
        """period >= 16 では決算整理仕訳を含む"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)
        make_journal(db, user.id, accounts["1010"].id, accounts["5010"].id,
                     1000, entry_date=date(2026, 12, 31), source="closing",
                     fiscal_period=16)

        compute_balance_cache(user.id, 2026, 16)
        db.session.commit()

        balances = get_cached_balances(user.id, 2026, 16)
        # closing 含むので借方1000 + 貸方1000 → 相殺
        d, c = balances.get(accounts["5010"].id, (0, 0))
        assert d == 1000
        assert c == 1000

    def test_update_existing_cache(self, app, user, accounts):
        """既存のキャッシュがある場合は更新"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)
        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

        # 追加の仕訳後に再計算
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     500, entry_date=date(2026, 1, 20), fiscal_period=1)
        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

        balances = get_cached_balances(user.id, 2026, 1)
        assert balances[accounts["5010"].id] == (1500, 0)

        # レコードが重複していないことを確認
        count = BalanceCache.query.filter_by(
            user_id=user.id, account_id=accounts["5010"].id,
            year=2026, period=1,
        ).count()
        assert count == 1

    def test_multiple_accounts(self, app, user, accounts):
        """複数の科目のキャッシュが正しく作成される"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)
        make_journal(db, user.id, accounts["5020"].id, accounts["1020"].id,
                     50000, entry_date=date(2026, 1, 15), fiscal_period=1)

        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

        balances = get_cached_balances(user.id, 2026, 1)
        assert balances[accounts["5010"].id] == (1000, 0)
        assert balances[accounts["5020"].id] == (50000, 0)
        assert balances[accounts["1010"].id] == (0, 1000)
        assert balances[accounts["1020"].id] == (0, 50000)

    def test_user_isolation(self, app, user, accounts, account_types):
        """他ユーザーの仕訳は含まれない"""
        from app.models.user import User
        user2 = User(username="user2", email="u2@example.com", user_type="personal")
        user2.set_password("pass")
        db.session.add(user2)
        db.session.flush()

        from app.models.account import Account
        a2_cash = Account(user_id=user2.id, account_type_id=account_types["asset"].id,
                          code="1010", name="現金", is_active=True)
        a2_food = Account(user_id=user2.id, account_type_id=account_types["expense"].id,
                          code="5010", name="食費", is_active=True)
        db.session.add_all([a2_cash, a2_food])
        db.session.commit()

        # user1 の仕訳
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)
        # user2 の仕訳
        make_journal(db, user2.id, a2_food.id, a2_cash.id,
                     9999, entry_date=date(2026, 1, 10), fiscal_period=1)

        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

        balances = get_cached_balances(user.id, 2026, 1)
        assert balances[accounts["5010"].id] == (1000, 0)


# ============================================================
# invalidate_balance_cache
# ============================================================

class TestInvalidateBalanceCache:
    def test_invalidate_from_period(self, app, user, accounts):
        """指定期間以降のキャッシュが削除される"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     2000, entry_date=date(2026, 2, 10), fiscal_period=2)

        compute_balance_cache(user.id, 2026, 1)
        compute_balance_cache(user.id, 2026, 2)
        db.session.commit()

        assert get_cached_balances(user.id, 2026, 1) != {}
        assert get_cached_balances(user.id, 2026, 2) != {}

        invalidate_balance_cache(user.id, 2026, 2)
        db.session.commit()

        # period 1 は残っている
        assert get_cached_balances(user.id, 2026, 1) != {}
        # period 2 は削除された
        assert get_cached_balances(user.id, 2026, 2) == {}

    def test_invalidate_all(self, app, user, accounts):
        """period=0 で全期間のキャッシュを削除"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)

        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()
        assert get_cached_balances(user.id, 2026, 1) != {}

        invalidate_balance_cache(user.id, 2026, 0)
        db.session.commit()
        assert get_cached_balances(user.id, 2026, 1) == {}

    def test_invalidate_different_year_unaffected(self, app, user, accounts):
        """他年度のキャッシュは影響されない"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2025, 1, 10), fiscal_period=1)
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     2000, entry_date=date(2026, 1, 10), fiscal_period=1)

        compute_balance_cache(user.id, 2025, 1)
        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

        invalidate_balance_cache(user.id, 2026, 0)
        db.session.commit()

        # 2025 年は残っている
        assert get_cached_balances(user.id, 2025, 1) != {}
        # 2026 年は削除された
        assert get_cached_balances(user.id, 2026, 1) == {}

    def test_invalidate_user_isolation(self, app, user, accounts, account_types):
        """他ユーザーのキャッシュは影響されない"""
        from app.models.user import User
        from app.models.account import Account

        user2 = User(username="user2", email="u2@example.com", user_type="personal")
        user2.set_password("pass")
        db.session.add(user2)
        db.session.flush()
        a2_cash = Account(user_id=user2.id, account_type_id=account_types["asset"].id,
                          code="1010", name="現金", is_active=True)
        a2_food = Account(user_id=user2.id, account_type_id=account_types["expense"].id,
                          code="5010", name="食費", is_active=True)
        db.session.add_all([a2_cash, a2_food])
        db.session.commit()

        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)
        make_journal(db, user2.id, a2_food.id, a2_cash.id,
                     2000, entry_date=date(2026, 1, 10), fiscal_period=1)

        compute_balance_cache(user.id, 2026, 1)
        compute_balance_cache(user2.id, 2026, 1)
        db.session.commit()

        invalidate_balance_cache(user.id, 2026, 0)
        db.session.commit()

        assert get_cached_balances(user.id, 2026, 1) == {}
        assert get_cached_balances(user2.id, 2026, 1) != {}


# ============================================================
# get_cached_balances
# ============================================================

class TestGetCachedBalances:
    def test_empty(self, app, user):
        """キャッシュがない場合は空辞書"""
        assert get_cached_balances(user.id, 2026, 1) == {}

    def test_returns_dict(self, app, user, accounts):
        """正しい形式の辞書を返す"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1500, entry_date=date(2026, 1, 10), fiscal_period=1)
        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

        balances = get_cached_balances(user.id, 2026, 1)
        assert isinstance(balances, dict)
        for account_id, (d, c) in balances.items():
            assert isinstance(account_id, int)
            assert isinstance(d, int)
            assert isinstance(c, int)

    def test_different_period_returns_different_cache(self, app, user, accounts):
        """異なる期間のキャッシュを区別して返す"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 10), fiscal_period=1)

        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

        assert get_cached_balances(user.id, 2026, 1) != {}
        assert get_cached_balances(user.id, 2026, 2) == {}
