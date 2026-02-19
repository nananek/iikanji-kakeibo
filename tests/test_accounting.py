"""accounting.py のテスト — 仕訳作成・残高バランス・出納帳・振替"""

from datetime import date

import pytest

from app.services.accounting import (
    create_cashbook_entry,
    create_journal_entry,
    create_transfer_entry,
    get_next_entry_number,
    update_cashbook_entry,
)


class TestGetNextEntryNumber:
    def test_first_entry(self, db, user, accounts):
        assert get_next_entry_number(user.id) == 1

    def test_increments(self, db, user, accounts):
        from tests.conftest import make_journal
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id, 1000)
        assert get_next_entry_number(user.id) == 2

    def test_per_user_isolation(self, db, user, accounts, auditor):
        from tests.conftest import make_journal
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id, 1000)
        assert get_next_entry_number(auditor.id) == 1


class TestCreateJournalEntry:
    def test_balanced_entry(self, db, user, accounts):
        entry = create_journal_entry(
            user_id=user.id,
            date=date(2026, 2, 15),
            description="食材購入",
            lines_data=[
                {"account_id": accounts["5010"].id, "debit_amount": 3000, "credit_amount": 0},
                {"account_id": accounts["1010"].id, "debit_amount": 0, "credit_amount": 3000},
            ],
        )
        assert entry.id is not None
        assert entry.is_balanced
        assert entry.total_debit == 3000
        assert entry.total_credit == 3000
        assert entry.entry_number == 1
        assert entry.source == "journal"

    def test_unbalanced_raises(self, db, user, accounts):
        with pytest.raises(ValueError, match="一致"):
            create_journal_entry(
                user_id=user.id,
                date=date(2026, 2, 15),
                description="不正仕訳",
                lines_data=[
                    {"account_id": accounts["5010"].id, "debit_amount": 3000, "credit_amount": 0},
                    {"account_id": accounts["1010"].id, "debit_amount": 0, "credit_amount": 2000},
                ],
            )

    def test_multi_line_entry(self, db, user, accounts):
        entry = create_journal_entry(
            user_id=user.id,
            date=date(2026, 2, 25),
            description="給与",
            lines_data=[
                {"account_id": accounts["1020"].id, "debit_amount": 250000, "credit_amount": 0},
                {"account_id": accounts["5020"].id, "debit_amount": 50000, "credit_amount": 0},
                {"account_id": accounts["4010"].id, "debit_amount": 0, "credit_amount": 300000},
            ],
        )
        assert entry.is_balanced
        assert len(entry.lines) == 3
        assert entry.total_debit == 300000

    def test_custom_source(self, db, user, accounts):
        entry = create_journal_entry(
            user_id=user.id,
            date=date(2026, 2, 15),
            description="CSV取込",
            lines_data=[
                {"account_id": accounts["5010"].id, "debit_amount": 500, "credit_amount": 0},
                {"account_id": accounts["1010"].id, "debit_amount": 0, "credit_amount": 500},
            ],
            source="csv",
        )
        assert entry.source == "csv"

    def test_batch_id(self, db, user, accounts):
        entry = create_journal_entry(
            user_id=user.id,
            date=date(2026, 2, 15),
            description="バッチ",
            lines_data=[
                {"account_id": accounts["5010"].id, "debit_amount": 100, "credit_amount": 0},
                {"account_id": accounts["1010"].id, "debit_amount": 0, "credit_amount": 100},
            ],
            batch_id="batch-001",
        )
        assert entry.batch_id == "batch-001"

    def test_fiscal_period_override(self, db, user, accounts):
        entry = create_journal_entry(
            user_id=user.id,
            date=date(2026, 12, 31),
            description="決算整理",
            lines_data=[
                {"account_id": accounts["5010"].id, "debit_amount": 100, "credit_amount": 0},
                {"account_id": accounts["1010"].id, "debit_amount": 0, "credit_amount": 100},
            ],
            fiscal_period=13,
        )
        assert entry.fiscal_period == 13

    def test_line_description(self, db, user, accounts):
        entry = create_journal_entry(
            user_id=user.id,
            date=date(2026, 2, 15),
            description="日用品",
            lines_data=[
                {"account_id": accounts["5010"].id, "debit_amount": 500, "credit_amount": 0,
                 "description": "洗剤"},
                {"account_id": accounts["1010"].id, "debit_amount": 0, "credit_amount": 500},
            ],
        )
        descs = [line.description for line in entry.lines]
        assert "洗剤" in descs

    def test_sequential_entry_numbers(self, db, user, accounts):
        e1 = create_journal_entry(
            user_id=user.id, date=date(2026, 1, 1), description="1件目",
            lines_data=[
                {"account_id": accounts["5010"].id, "debit_amount": 100, "credit_amount": 0},
                {"account_id": accounts["1010"].id, "debit_amount": 0, "credit_amount": 100},
            ],
        )
        e2 = create_journal_entry(
            user_id=user.id, date=date(2026, 1, 2), description="2件目",
            lines_data=[
                {"account_id": accounts["5010"].id, "debit_amount": 200, "credit_amount": 0},
                {"account_id": accounts["1010"].id, "debit_amount": 0, "credit_amount": 200},
            ],
        )
        assert e2.entry_number == e1.entry_number + 1


class TestCreateCashbookEntry:
    def test_expense(self, db, user, accounts):
        entry = create_cashbook_entry(
            user_id=user.id,
            date=date(2026, 2, 15),
            transaction_type="expense",
            payment_account_id=accounts["1010"].id,
            category_account_id=accounts["5010"].id,
            amount=2000,
            description="コンビニ",
        )
        assert entry.is_balanced
        assert entry.source == "cashbook"
        assert len(entry.lines) == 2
        # 費用は 借方:費目 / 貸方:支払元
        debits = [l for l in entry.lines if l.debit_amount > 0]
        credits = [l for l in entry.lines if l.credit_amount > 0]
        assert debits[0].account_id == accounts["5010"].id
        assert credits[0].account_id == accounts["1010"].id

    def test_income(self, db, user, accounts):
        entry = create_cashbook_entry(
            user_id=user.id,
            date=date(2026, 2, 25),
            transaction_type="income",
            payment_account_id=accounts["1020"].id,
            category_account_id=accounts["4010"].id,
            amount=300000,
            description="給与",
        )
        assert entry.is_balanced
        # 収入は 借方:入金先 / 貸方:収入科目
        debits = [l for l in entry.lines if l.debit_amount > 0]
        credits = [l for l in entry.lines if l.credit_amount > 0]
        assert debits[0].account_id == accounts["1020"].id
        assert credits[0].account_id == accounts["4010"].id


class TestCreateTransferEntry:
    def test_transfer(self, db, user, accounts):
        entry = create_transfer_entry(
            user_id=user.id,
            date=date(2026, 2, 15),
            from_account_id=accounts["1020"].id,
            to_account_id=accounts["1010"].id,
            amount=50000,
            description="ATM引出",
        )
        assert entry.is_balanced
        assert entry.total_debit == 50000
        debits = [l for l in entry.lines if l.debit_amount > 0]
        credits = [l for l in entry.lines if l.credit_amount > 0]
        assert debits[0].account_id == accounts["1010"].id   # to
        assert credits[0].account_id == accounts["1020"].id  # from


class TestUpdateCashbookEntry:
    def test_update_amount(self, db, user, accounts):
        entry = create_cashbook_entry(
            user_id=user.id, date=date(2026, 2, 15),
            transaction_type="expense",
            payment_account_id=accounts["1010"].id,
            category_account_id=accounts["5010"].id,
            amount=1000, description="元",
        )
        updated = update_cashbook_entry(
            entry, date=date(2026, 2, 16),
            transaction_type="expense",
            payment_account_id=accounts["1010"].id,
            category_account_id=accounts["5010"].id,
            amount=2000, description="変更後",
        )
        assert updated.is_balanced
        assert updated.total_debit == 2000
        assert updated.description == "変更後"
        assert updated.date == date(2026, 2, 16)

    def test_update_category(self, db, user, accounts):
        entry = create_cashbook_entry(
            user_id=user.id, date=date(2026, 2, 15),
            transaction_type="expense",
            payment_account_id=accounts["1010"].id,
            category_account_id=accounts["5010"].id,
            amount=1000, description="元",
        )
        updated = update_cashbook_entry(
            entry, date=date(2026, 2, 15),
            transaction_type="expense",
            payment_account_id=accounts["1010"].id,
            category_account_id=accounts["5020"].id,  # 食費→住居費
            amount=1000, description="変更",
        )
        debits = [l for l in updated.lines if l.debit_amount > 0]
        assert debits[0].account_id == accounts["5020"].id
