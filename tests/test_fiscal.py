"""fiscal.py のテスト — 期間ロック・年度管理・月次確定・損益振替"""

from datetime import date, datetime, timezone

import pytest

from app.extensions import db as _db
from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry
from app.services.fiscal import (
    check_entry_modifiable,
    check_period_open_for_new,
    close_period,
    delete_closing_entries,
    get_capital_account_code,
    get_closed_period,
    get_effective_period,
    get_restricted_before_year,
    is_period_locked,
    is_year_open,
    reopen_period,
)
from tests.conftest import make_journal


class TestGetClosedPeriod:
    def test_no_record(self, db, user):
        assert get_closed_period(user.id, 2026) == -1

    def test_with_record(self, db, user):
        fc = FiscalClose(user_id=user.id, year=2026, closed_period=3)
        db.session.add(fc)
        db.session.commit()
        assert get_closed_period(user.id, 2026) == 3


class TestIsPeriodLocked:
    def test_not_locked_when_no_close(self, db, user):
        assert is_period_locked(user.id, 2026, 1) is False

    def test_locked_when_closed(self, db, user):
        fc = FiscalClose(user_id=user.id, year=2026, closed_period=3)
        db.session.add(fc)
        db.session.commit()
        assert is_period_locked(user.id, 2026, 1) is True
        assert is_period_locked(user.id, 2026, 3) is True
        assert is_period_locked(user.id, 2026, 4) is False


class TestIsYearOpen:
    def test_current_year_open(self, db, user):
        """created_at=2025-01-01 なので 2024以降は常にオープン"""
        assert is_year_open(user.id, 2025) is True
        assert is_year_open(user.id, 2026) is True

    def test_old_year_closed(self, db, user):
        """2023年以前はFiscalCloseがないと閉じている"""
        assert is_year_open(user.id, 2023) is False

    def test_old_year_opened_with_fiscal_close(self, db, user):
        fc = FiscalClose(user_id=user.id, year=2023, closed_period=-1)
        db.session.add(fc)
        db.session.commit()
        assert is_year_open(user.id, 2023) is True


class TestGetRestrictedBeforeYear:
    def test_returns_threshold(self, db, user):
        """created_at=2025 → year-1=2024、2024未満が制限"""
        result = get_restricted_before_year(user.id)
        assert result == 2024


class TestCheckEntryModifiable:
    def test_modifiable_when_not_locked(self, db, user, accounts):
        entry = make_journal(db, user.id, "5010", "1010",
                             1000, entry_date=date(2026, 2, 15))
        assert check_entry_modifiable(user.id, entry) is None

    def test_not_modifiable_when_locked(self, db, user, accounts):
        fc = FiscalClose(user_id=user.id, year=2026, closed_period=2)
        db.session.add(fc)
        db.session.commit()
        entry = make_journal(db, user.id, "5010", "1010",
                             1000, entry_date=date(2026, 2, 15))
        result = check_entry_modifiable(user.id, entry)
        assert result is not None

    def test_closing_entry_not_modifiable(self, db, user, accounts):
        entry = make_journal(db, user.id, "5010", "1010",
                             1000, entry_date=date(2026, 12, 31), source="closing")
        result = check_entry_modifiable(user.id, entry)
        assert result is not None

    def test_closing_detected_by_is_closing_not_source(self, db, user, accounts):
        # E3-F: source ではなく is_closing フラグで closing 判定する。
        entry = make_journal(db, user.id, "5010", "1010",
                             1000, entry_date=date(2026, 2, 15))
        # E3-F PR-D-6-5: source 列は DROP 済。is_closing=True のみで弾く
        # (旧 source != "closing" でも is_closing で判定されることを確認)。
        entry.is_closing = True
        db.session.commit()
        result = check_entry_modifiable(user.id, entry)
        assert result is not None


class TestCheckPeriodOpenForNew:
    def test_open_period(self, db, user):
        assert check_period_open_for_new(user.id, 2026, 2) is None

    def test_locked_period(self, db, user):
        fc = FiscalClose(user_id=user.id, year=2026, closed_period=3)
        db.session.add(fc)
        db.session.commit()
        result = check_period_open_for_new(user.id, 2026, 2)
        assert result is not None

    def test_year_not_open(self, db, user):
        result = check_period_open_for_new(user.id, 2020, 1)
        assert result is not None


class TestGetCapitalAccountCode:
    def test_found(self, db, user, accounts):
        result = get_capital_account_code(user.id)
        assert result == accounts["3010"].code

    def test_not_found(self, db, user, account_types):
        """科目がない場合は None"""
        assert get_capital_account_code(user.id) is None


class TestClosePeriod:
    def test_close_opening_period(self, db, user, accounts):
        """期首振戻月 (period 0) を最初に確定"""
        result = close_period(user.id, 2026, 0)
        assert result is None
        assert get_closed_period(user.id, 2026) == 0

    def test_close_first_month(self, db, user, accounts):
        make_journal(db, user.id, "5010", "1010",
                     1000, entry_date=date(2026, 1, 15))
        close_period(user.id, 2026, 0)  # 先に期首を確定
        result = close_period(user.id, 2026, 1)
        assert result is None
        assert get_closed_period(user.id, 2026) == 1

    def test_close_sequential(self, db, user, accounts):
        make_journal(db, user.id, "5010", "1010",
                     1000, entry_date=date(2026, 1, 15))
        close_period(user.id, 2026, 0)
        close_period(user.id, 2026, 1)
        result = close_period(user.id, 2026, 2)
        assert result is None
        assert get_closed_period(user.id, 2026) == 2

    def test_cannot_skip_period(self, db, user, accounts):
        result = close_period(user.id, 2026, 3)
        assert result is not None  # エラーメッセージ

    def test_period15_rejected(self, db, user, accounts):
        """#338 item1: 決算月3 (period15) は専用エンドポイント close-closing 経由のみ。
        close_period は period14 確定済みでも 15 を拒否し FiscalClose を進めない
        (view を経由しない caller への多層防御)。"""
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=14))
        db.session.commit()
        result = close_period(user.id, 2026, 15)
        assert result is not None
        assert "close-closing" in result
        fc = FiscalClose.query.filter_by(user_id=user.id, year=2026).first()
        assert fc.closed_period == 14


class TestReopenPeriod:
    def test_reopen_last_closed(self, db, user, accounts):
        make_journal(db, user.id, "5010", "1010",
                     1000, entry_date=date(2026, 1, 15))
        close_period(user.id, 2026, 0)
        close_period(user.id, 2026, 1)
        result = reopen_period(user.id, 2026, 1)
        assert result is None
        assert get_closed_period(user.id, 2026) == 0

    def test_cannot_reopen_non_last(self, db, user, accounts):
        make_journal(db, user.id, "5010", "1010",
                     1000, entry_date=date(2026, 1, 15))
        close_period(user.id, 2026, 0)
        close_period(user.id, 2026, 1)
        close_period(user.id, 2026, 2)
        result = reopen_period(user.id, 2026, 1)
        assert result is not None


class TestGetEffectivePeriod:
    def test_from_date(self, db, user, accounts):
        entry = make_journal(db, user.id, "5010", "1010",
                             1000, entry_date=date(2026, 3, 15))
        assert get_effective_period(entry) == 3

    def test_from_fiscal_period(self, db, user, accounts):
        entry = make_journal(db, user.id, "5010", "1010",
                             1000, entry_date=date(2026, 12, 31), fiscal_period=13)
        assert get_effective_period(entry) == 13


class TestDeleteClosingEntries:
    """#338 item1: サーバ側 generate_closing_entries は撤去 (closing 生成・暗号化は
    クライアント closing.js → POST /api/v1/fiscal/close-closing に移譲)。
    delete_closing_entries は reopen と専用エンドポイントの冪等削除で引き続き使う。
    closing 集計ロジックの単体テストは tests/static/js/test_closing.mjs にある。"""

    def _make_closing(self, db, user_id, year=2026):
        from app.models.journal import JournalEntryLine
        e = JournalEntry(user_id=user_id, entry_number=1, is_closing=True,
                         fiscal_month=16, fiscal_year=year,
                         encrypted_blob=b"x", blob_iv=bytes(12))
        e.lines = [
            JournalEntryLine(account_user_id=user_id, account_code="4010",
                             debit_amount=100, credit_amount=0),
            JournalEntryLine(account_user_id=user_id, account_code="3020",
                             debit_amount=0, credit_amount=100),
        ]
        db.session.add(e)
        db.session.commit()
        return e

    def test_deletes_closing_entry(self, db, user, accounts):
        self._make_closing(db, user.id)
        assert JournalEntry.query.filter_by(user_id=user.id, is_closing=True).count() == 1
        delete_closing_entries(user.id, 2026)
        db.session.commit()
        assert JournalEntry.query.filter_by(user_id=user.id, is_closing=True).count() == 0

    def test_delete_no_closing_is_noop(self, db, user, accounts):
        delete_closing_entries(user.id, 2026)  # 例外を投げない
