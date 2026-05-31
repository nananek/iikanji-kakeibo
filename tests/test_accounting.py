"""accounting.py のテスト — 仕訳作成・残高バランス"""

import pytest

from app.services.accounting import (
    create_journal_entry,
    get_next_entry_number,
)


class TestGetNextEntryNumber:
    def test_first_entry(self, db, user, accounts):
        assert get_next_entry_number(user.id) == 1

    def test_increments(self, db, user, accounts):
        from tests.conftest import make_journal
        make_journal(db, user.id, "5010", "1010", 1000)
        assert get_next_entry_number(user.id) == 2

    def test_per_user_isolation(self, db, user, accounts, auditor):
        from tests.conftest import make_journal
        make_journal(db, user.id, "5010", "1010", 1000)
        assert get_next_entry_number(auditor.id) == 1


class TestCreateJournalEntry:
    # E3-F PR-D-6-6: wire 平文除去後、create_journal_entry の平文メタは
    # fiscal_year / fiscal_month のみ (date / description / source / fiscal_period
    # 引数は撤去)。entry 本体はクライアントが暗号化して encrypted_blob に格納する。
    def test_balanced_entry(self, db, user, accounts):
        entry = create_journal_entry(
            user_id=user.id,
            lines_data=[
                {"account_code": accounts["5010"].code, "debit_amount": 3000, "credit_amount": 0},
                {"account_code": accounts["1010"].code, "debit_amount": 0, "credit_amount": 3000},
            ],
            fiscal_year=2026, fiscal_month=2,
        )
        assert entry.id is not None
        assert entry.is_balanced
        assert entry.total_debit == 3000
        assert entry.total_credit == 3000
        assert entry.entry_number == 1
        assert entry.fiscal_year == 2026
        assert entry.fiscal_month == 2

    def test_unbalanced_raises(self, db, user, accounts):
        with pytest.raises(ValueError, match="一致"):
            create_journal_entry(
                user_id=user.id,
                lines_data=[
                    {"account_code": accounts["5010"].code, "debit_amount": 3000, "credit_amount": 0},
                    {"account_code": accounts["1010"].code, "debit_amount": 0, "credit_amount": 2000},
                ],
                fiscal_year=2026, fiscal_month=2,
            )

    def test_multi_line_entry(self, db, user, accounts):
        entry = create_journal_entry(
            user_id=user.id,
            lines_data=[
                {"account_code": accounts["1020"].code, "debit_amount": 250000, "credit_amount": 0},
                {"account_code": accounts["5020"].code, "debit_amount": 50000, "credit_amount": 0},
                {"account_code": accounts["4010"].code, "debit_amount": 0, "credit_amount": 300000},
            ],
            fiscal_year=2026, fiscal_month=2,
        )
        assert entry.is_balanced
        assert len(entry.lines) == 3
        assert entry.total_debit == 300000

    def test_batch_id(self, db, user, accounts):
        entry = create_journal_entry(
            user_id=user.id,
            lines_data=[
                {"account_code": accounts["5010"].code, "debit_amount": 100, "credit_amount": 0},
                {"account_code": accounts["1010"].code, "debit_amount": 0, "credit_amount": 100},
            ],
            fiscal_year=2026, fiscal_month=2,
            batch_id="batch-001",
        )
        assert entry.batch_id == "batch-001"

    def test_fiscal_month_special_period(self, db, user, accounts):
        # 決算整理 (fiscal_month=13) はクライアントが算出して渡す。
        entry = create_journal_entry(
            user_id=user.id,
            lines_data=[
                {"account_code": accounts["5010"].code, "debit_amount": 100, "credit_amount": 0},
                {"account_code": accounts["1010"].code, "debit_amount": 0, "credit_amount": 100},
            ],
            fiscal_year=2026, fiscal_month=13,
        )
        assert entry.fiscal_month == 13

    def test_line_extra_keys_ignored(self, db, user, accounts):
        # lines_data に余分な description キーがあっても無視される (列は DROP 済)。
        entry = create_journal_entry(
            user_id=user.id,
            lines_data=[
                {"account_code": accounts["5010"].code, "debit_amount": 500, "credit_amount": 0,
                 "description": "洗剤"},
                {"account_code": accounts["1010"].code, "debit_amount": 0, "credit_amount": 500},
            ],
            fiscal_year=2026, fiscal_month=2,
        )
        assert len(entry.lines) == 2

    def test_sequential_entry_numbers(self, db, user, accounts):
        e1 = create_journal_entry(
            user_id=user.id,
            lines_data=[
                {"account_code": accounts["5010"].code, "debit_amount": 100, "credit_amount": 0},
                {"account_code": accounts["1010"].code, "debit_amount": 0, "credit_amount": 100},
            ],
            fiscal_year=2026, fiscal_month=1,
        )
        e2 = create_journal_entry(
            user_id=user.id,
            lines_data=[
                {"account_code": accounts["5010"].code, "debit_amount": 200, "credit_amount": 0},
                {"account_code": accounts["1010"].code, "debit_amount": 0, "credit_amount": 200},
            ],
            fiscal_year=2026, fiscal_month=1,
        )
        assert e2.entry_number == e1.entry_number + 1
