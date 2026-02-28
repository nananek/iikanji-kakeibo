"""CSV明細照合（マッチング）サービスのテスト"""

from datetime import date

import pytest

from tests.conftest import make_journal
from app.services.reconciliation import find_matches


@pytest.fixture
def cc_account(accounts):
    """クレジットカード科目（支払元口座として使用）"""
    return accounts["2010"]


class TestFindMatches:
    """find_matches() のユニットテスト"""

    def test_exact_match(self, db, user, accounts, cc_account):
        """金額・日付完全一致 → matched"""
        make_journal(
            db, user.id,
            acct_debit_id=accounts["5010"].id,   # 食費
            acct_credit_id=cc_account.id,          # CC
            amount=1500,
            entry_date=date(2026, 1, 10),
            description="コンビニ",
            source="ai_receipt",
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "コンビニ", "withdrawal": 1500, "deposit": 0},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert len(results) == 1
        assert results[0]["status"] == "matched"
        assert results[0]["csv_index"] == 0
        assert len(results[0]["matches"]) == 1
        m = results[0]["matches"][0]
        assert m["amount"] == 1500
        assert m["date"] == "2026-01-10"
        assert m["source"] == "ai_receipt"
        assert "食費" in m["category_name"]

    def test_date_within_tolerance(self, db, user, accounts, cc_account):
        """金額一致・日付+3日 → matched"""
        make_journal(
            db, user.id,
            acct_debit_id=accounts["5010"].id,
            acct_credit_id=cc_account.id,
            amount=2000,
            entry_date=date(2026, 1, 13),
            source="cashbook",
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "スーパー", "withdrawal": 2000, "deposit": 0},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert results[0]["status"] == "matched"
        assert len(results[0]["matches"]) == 1

    def test_date_outside_tolerance(self, db, user, accounts, cc_account):
        """金額一致・日付+6日 → unmatched（許容範囲外）"""
        make_journal(
            db, user.id,
            acct_debit_id=accounts["5010"].id,
            acct_credit_id=cc_account.id,
            amount=3000,
            entry_date=date(2026, 1, 16),
            source="cashbook",
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "ドラッグストア", "withdrawal": 3000, "deposit": 0},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert results[0]["status"] == "unmatched"
        assert results[0]["matches"] == []

    def test_multiple_candidates(self, db, user, accounts, cc_account):
        """同金額の仕訳が2件 → multiple"""
        for d in (date(2026, 1, 9), date(2026, 1, 11)):
            make_journal(
                db, user.id,
                acct_debit_id=accounts["5010"].id,
                acct_credit_id=cc_account.id,
                amount=1000,
                entry_date=d,
                source="cashbook",
            )
        csv_rows = [
            {"date": "2026-01-10", "description": "ランチ", "withdrawal": 1000, "deposit": 0},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert results[0]["status"] == "multiple"
        assert len(results[0]["matches"]) == 2

    def test_amount_mismatch(self, db, user, accounts, cc_account):
        """金額不一致 → unmatched"""
        make_journal(
            db, user.id,
            acct_debit_id=accounts["5010"].id,
            acct_credit_id=cc_account.id,
            amount=999,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "不一致", "withdrawal": 1000, "deposit": 0},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert results[0]["status"] == "unmatched"

    def test_deposit_direction(self, db, user, accounts, cc_account):
        """入金方向の一致 — deposit → debit_amount でマッチ"""
        # CC口座へのデビット = 返金（CC残高が減る）
        make_journal(
            db, user.id,
            acct_debit_id=cc_account.id,           # CC にデビット（返金）
            acct_credit_id=accounts["5010"].id,     # 食費を取消
            amount=500,
            entry_date=date(2026, 1, 10),
            source="journal",
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "返金", "withdrawal": 0, "deposit": 500},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert results[0]["status"] == "matched"
        assert results[0]["matches"][0]["amount"] == 500

    def test_no_date_in_csv_row(self, db, user, accounts, cc_account):
        """CSV行に日付なし → unmatched"""
        make_journal(
            db, user.id,
            acct_debit_id=accounts["5010"].id,
            acct_credit_id=cc_account.id,
            amount=1000,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": None, "description": "日付なし", "withdrawal": 1000, "deposit": 0},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert results[0]["status"] == "unmatched"

    def test_no_duplicate_match(self, db, user, accounts, cc_account):
        """1仕訳が複数CSV行に重複マッチしない — 先にマッチした行が優先"""
        make_journal(
            db, user.id,
            acct_debit_id=accounts["5010"].id,
            acct_credit_id=cc_account.id,
            amount=800,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "1行目", "withdrawal": 800, "deposit": 0},
            {"date": "2026-01-10", "description": "2行目", "withdrawal": 800, "deposit": 0},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert results[0]["status"] == "matched"
        assert results[1]["status"] == "unmatched"

    def test_empty_csv_rows(self, db, user, accounts, cc_account):
        """空のCSV行リスト → 空リスト"""
        results = find_matches(user.id, cc_account.id, [])
        assert results == []

    def test_zero_amount_row(self, db, user, accounts, cc_account):
        """入出金ゼロ → unmatched"""
        csv_rows = [
            {"date": "2026-01-10", "description": "ゼロ", "withdrawal": 0, "deposit": 0},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert results[0]["status"] == "unmatched"

    def test_matches_sorted_by_date_proximity(self, db, user, accounts, cc_account):
        """複数候補は日付が近い順にソートされる"""
        make_journal(
            db, user.id,
            acct_debit_id=accounts["5010"].id,
            acct_credit_id=cc_account.id,
            amount=1200,
            entry_date=date(2026, 1, 14),  # +4日
        )
        make_journal(
            db, user.id,
            acct_debit_id=accounts["5010"].id,
            acct_credit_id=cc_account.id,
            amount=1200,
            entry_date=date(2026, 1, 11),  # +1日
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "テスト", "withdrawal": 1200, "deposit": 0},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert results[0]["status"] == "multiple"
        # 日付が近い方(1/11)が先
        assert results[0]["matches"][0]["date"] == "2026-01-11"
        assert results[0]["matches"][1]["date"] == "2026-01-14"

    def test_other_user_entries_not_matched(self, db, user, accounts, cc_account,
                                            second_user, second_user_accounts):
        """他ユーザーの仕訳はマッチしない"""
        make_journal(
            db, second_user.id,
            acct_debit_id=second_user_accounts["5010"].id,
            acct_credit_id=second_user_accounts["1010"].id,
            amount=1000,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "他人", "withdrawal": 1000, "deposit": 0},
        ]
        results = find_matches(user.id, cc_account.id, csv_rows)
        assert results[0]["status"] == "unmatched"
