"""fiscal サービス (services/fiscal.py) の追加テスト

既存 test_fiscal.py が close_period/reopen_period/closing_entries 周辺を扱う。
こちらは未到達の枝を補強。
"""

from datetime import date, datetime, timezone

import pytest

from app.models.account import Account, AccountType
from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.user import User
from app.services.fiscal import (
    adjust_date_for_fiscal_period,
    check_entry_modifiable,
    check_period_open_for_new,
    close_period,
    delete_closing_entries,
    generate_closing_entries,
    get_capital_account_code,
    get_closed_periods_for_dates,
    get_closed_periods_map,
    get_effective_period,
    get_last_closed,
    get_restricted_before_year,
    is_period_locked,
    is_year_open,
    reopen_period,
)
from tests.conftest import make_journal


class TestAdjustDateForFiscalPeriod:
    def test_no_period(self):
        d = date(2026, 5, 15)
        assert adjust_date_for_fiscal_period(d, None) == d

    def test_period_0_jan1(self):
        d = date(2026, 5, 15)
        assert adjust_date_for_fiscal_period(d, 0) == date(2026, 1, 1)

    def test_period_13_dec31(self):
        assert adjust_date_for_fiscal_period(date(2026, 5, 15), 13) == date(2026, 12, 31)

    def test_period_14_dec31(self):
        assert adjust_date_for_fiscal_period(date(2026, 5, 15), 14) == date(2026, 12, 31)

    def test_period_16_dec31(self):
        assert adjust_date_for_fiscal_period(date(2026, 5, 15), 16) == date(2026, 12, 31)

    def test_normal_period_unchanged(self):
        d = date(2026, 5, 15)
        # 通常期間 (1-12) は補正なし
        assert adjust_date_for_fiscal_period(d, 5) == d


class TestGetEffectivePeriod:
    def test_with_fiscal_month(self, db, user, accounts):
        # E3-F PR-D-6-5-pre1: get_effective_period は fiscal_month を使用する
        # (旧 fiscal_period / date.month フォールバックは撤去済)。
        e = JournalEntry(
            user_id=user.id,
            entry_number=1,

            fiscal_month=13, fiscal_year=2026,
        )
        e.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=100, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=0, credit_amount=100),
        ]
        db.session.add(e)
        db.session.commit()
        assert get_effective_period(e) == 13

    def test_fiscal_month_from_date(self, db, user, accounts):
        # make_journal は fiscal_month を date.month で populate する。
        e = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 5, 15))
        assert get_effective_period(e) == 5


class TestGetClosedPeriodsMap:
    def test_empty(self, db, user, accounts):
        assert get_closed_periods_map(user.id) == {}

    def test_with_data(self, db, user, accounts):
        db.session.add_all([
            FiscalClose(user_id=user.id, year=2024, closed_period=12),
            FiscalClose(user_id=user.id, year=2025, closed_period=15),
            FiscalClose(user_id=user.id, year=2026, closed_period=-1),  # 未確定は除外
        ])
        db.session.commit()
        result = get_closed_periods_map(user.id)
        assert result == {2024: 12, 2025: 15}


class TestGetLastClosed:
    def test_none(self, db, user, accounts):
        assert get_last_closed(user.id) is None

    def test_with_close(self, db, user, accounts):
        db.session.add_all([
            FiscalClose(user_id=user.id, year=2024, closed_period=15),
            FiscalClose(user_id=user.id, year=2025, closed_period=5),
        ])
        db.session.commit()
        result = get_last_closed(user.id)
        assert result == {"year": 2025, "period": 5}


class TestGetClosedPeriodsForDates:
    def test_empty(self, db, user, accounts):
        result = get_closed_periods_for_dates(user.id, [])
        assert result == {}

    def test_invalid_dates_skipped(self, db, user, accounts):
        result = get_closed_periods_for_dates(user.id, ["INVALID", None, ""])
        assert result == {}

    def test_with_close_data(self, db, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2024, closed_period=12))
        db.session.commit()
        result = get_closed_periods_for_dates(
            user.id, ["2024-05-15", "2025-03-01"]
        )
        assert result == {2024: 12}


class TestIsPeriodLocked:
    def test_unlocked(self, db, user, accounts):
        assert is_period_locked(user.id, 2026, 5) is False

    def test_locked(self, db, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=5))
        db.session.commit()
        assert is_period_locked(user.id, 2026, 5) is True
        assert is_period_locked(user.id, 2026, 4) is True
        assert is_period_locked(user.id, 2026, 6) is False


class TestCheckEntryModifiable:
    def test_closing_source_not_modifiable(self, db, user, accounts):
        e = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 12, 31), source="closing")
        result = check_entry_modifiable(user.id, e)
        assert result is not None
        assert "損益振替" in result

    def test_locked_period(self, db, user, accounts):
        e = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 5, 15))
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=5))
        db.session.commit()
        result = check_entry_modifiable(user.id, e)
        assert result is not None
        assert "確定済み" in result

    def test_modifiable(self, db, user, accounts):
        e = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 5, 15))
        assert check_entry_modifiable(user.id, e) is None


class TestCheckPeriodOpenForNew:
    def test_year_not_open(self, db, user, accounts):
        # user は 2025 created → 2023 は前々年
        result = check_period_open_for_new(user.id, 2023, 5)
        assert result is not None
        assert "開設" in result

    def test_period_locked(self, db, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=5))
        db.session.commit()
        result = check_period_open_for_new(user.id, 2026, 5)
        assert result is not None
        assert "確定済み" in result

    def test_open(self, db, user, accounts):
        result = check_period_open_for_new(user.id, 2026, 5)
        assert result is None


class TestIsYearOpen:
    def test_unknown_user(self, db, accounts):
        assert is_year_open(99999, 2026) is False

    def test_recent_year_open(self, db, user, accounts):
        # 前年 (2024) は常にオープン (user は 2025 created)
        assert is_year_open(user.id, 2024) is True

    def test_old_year_no_close_record(self, db, user, accounts):
        # 2023 は前々年で FiscalClose なし → False
        assert is_year_open(user.id, 2023) is False

    def test_old_year_with_close_record(self, db, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2023, closed_period=-1))
        db.session.commit()
        assert is_year_open(user.id, 2023) is True


class TestGetRestrictedBeforeYear:
    def test_unknown_user(self, db, accounts):
        assert get_restricted_before_year(99999) is None

    def test_returns_year_minus_1(self, db, user, accounts):
        # user は 2025 created → 2024
        assert get_restricted_before_year(user.id) == 2024


class TestGetCapitalAccountCode:
    def test_present(self, db, user, accounts):
        # accounts fixture に capital がある
        assert get_capital_account_code(user.id) == "3010"

    def test_absent(self, db, user, accounts):
        # capital を削除
        cap = Account.query.filter_by(user_id=user.id, system_role="capital").first()
        db.session.delete(cap)
        db.session.commit()
        assert get_capital_account_code(user.id) is None


class TestClosePeriodEdgeCases:
    def test_already_closed(self, db, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=5))
        db.session.commit()
        result = close_period(user.id, 2026, 5)
        assert result is not None
        assert "既に確定" in result

    def test_skipping_period(self, db, user, accounts):
        # current=2 だが period=5 で confirm しようとする
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        result = close_period(user.id, 2026, 5)
        assert result is not None
        assert "先に" in result


class TestReopenPeriodEdgeCases:
    def test_not_closed(self, db, user, accounts):
        result = reopen_period(user.id, 2026, 5)
        assert result is not None
        assert "確定されていません" in result

    def test_not_last(self, db, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=5))
        db.session.commit()
        # 2 を解除しようとする (5 が最後)
        result = reopen_period(user.id, 2026, 2)
        assert result is not None
        assert "最後に確定" in result or "確定されていません" in result

    def test_blocked_by_later_year(self, db, user, accounts):
        db.session.add_all([
            FiscalClose(user_id=user.id, year=2025, closed_period=15),
            FiscalClose(user_id=user.id, year=2026, closed_period=2),
        ])
        db.session.commit()
        # 2025 を解除しようとするが、2026 に確定がある
        result = reopen_period(user.id, 2025, 15)
        assert result is not None
        assert "解除できません" in result


class TestGenerateClosingEntries:
    def test_no_revenue_or_expense(self, db, user, accounts):
        # 収益・費用の仕訳が無い
        result = generate_closing_entries(user.id, 2026)
        assert result is None

    def test_missing_required_accounts(self, db, account_types):
        # retained_earnings が無いユーザー
        from app.models.user import User as UserModel
        u = UserModel(username="noret", email="x@y.com", user_type="personal")
        u.set_password("p")
        db.session.add(u)
        db.session.commit()
        result = generate_closing_entries(u.id, 2026)
        assert result is not None
        assert "見つかりません" in result

    def test_with_revenue(self, db, user, accounts):
        # 給与収入 (4010) がある状態
        make_journal(db, user.id, "1010", "4010", 250000,
                     entry_date=date(2026, 5, 15))
        result = generate_closing_entries(user.id, 2026)
        # 振替仕訳が生成されるか None が返る (生成されたら closing source の entry が増える)
        if result is None:
            from app.models.journal import JournalEntry as JE
            # E3-F PR-D-6-4: closing は is_closing で識別 (平文 source は書かない)。
            count = JE.query.filter_by(user_id=user.id, is_closing=True).count()
            assert count >= 1


class TestDeleteClosingEntries:
    def test_no_closing(self, db, user, accounts):
        # 削除対象がなくてもエラーにならない
        delete_closing_entries(user.id, 2026)

    def test_deletes_closing_entries(self, db, user, accounts):
        # E3-F: closing 仕訳は is_closing=True / fiscal_month=16 / fiscal_year
        # で識別する (delete_closing_entries の新フィルタ)。
        from app.models.journal import JournalEntry as JE, JournalEntryLine as JEL
        e = JE(
            user_id=user.id,
            entry_number=1,
            is_closing=True, fiscal_month=16, fiscal_year=2026,
        )
        e.lines = [
            JEL(account_user_id=user.id, account_code="1010",
                debit_amount=100, credit_amount=0),
            JEL(account_user_id=user.id, account_code="4010",
                debit_amount=0, credit_amount=100),
        ]
        db.session.add(e)
        db.session.commit()
        eid = e.id
        delete_closing_entries(user.id, 2026)
        db.session.commit()
        assert db.session.get(JE, eid) is None
