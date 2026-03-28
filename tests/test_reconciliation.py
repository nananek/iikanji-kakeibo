"""CSV明細照合（マッチング）サービスのテスト"""

from datetime import date
from unittest.mock import patch

import pytest

from tests.conftest import make_journal
from app.services.reconciliation import (
    find_matches, find_ai_matches, _format_csv_rows, _format_journal_rows,
)


@pytest.fixture
def cc_account(accounts):
    """クレジットカード科目（支払元口座として使用）"""
    return accounts["2010"]


class TestFindMatches:
    """find_matches() のユニットテスト"""

    def _csv_results(self, user_id, cc_code, csv_rows):
        """find_matches() を呼び csv_results だけ返すヘルパー"""
        return find_matches(user_id, cc_code, csv_rows)["csv_results"]

    def test_exact_match(self, db, user, accounts, cc_account):
        """金額・日付完全一致 → matched"""
        make_journal(
            db, user.id,
            acct_debit_code="5010",   # 食費
            acct_credit_code=cc_account.code,          # CC
            amount=1500,
            entry_date=date(2026, 1, 10),
            description="コンビニ",
            source="ai_receipt",
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "コンビニ", "withdrawal": 1500, "deposit": 0},
        ]
        results = self._csv_results(user.id, cc_account.code, csv_rows)
        assert len(results) == 1
        assert results[0]["status"] == "matched"
        assert results[0]["csv_index"] == 0
        assert len(results[0]["matches"]) == 1
        m = results[0]["matches"][0]
        assert m["amount"] == 1500
        assert m["date"] == "2026-01-10"
        assert m["source"] == "ai_receipt"
        assert "食費" in m["category_name"]

    def test_date_mismatch_unmatched(self, db, user, accounts, cc_account):
        """金額一致・日付不一致 → unmatched（日付完全一致のみ）"""
        make_journal(
            db, user.id,
            acct_debit_code="5010",
            acct_credit_code=cc_account.code,
            amount=2000,
            entry_date=date(2026, 1, 11),
            source="cashbook",
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "スーパー", "withdrawal": 2000, "deposit": 0},
        ]
        results = self._csv_results(user.id, cc_account.code, csv_rows)
        assert results[0]["status"] == "unmatched"

    def test_multiple_candidates(self, db, user, accounts, cc_account):
        """同日同金額の仕訳が2件 → multiple"""
        for _ in range(2):
            make_journal(
                db, user.id,
                acct_debit_code="5010",
                acct_credit_code=cc_account.code,
                amount=1000,
                entry_date=date(2026, 1, 10),
                source="cashbook",
            )
        csv_rows = [
            {"date": "2026-01-10", "description": "ランチ", "withdrawal": 1000, "deposit": 0},
        ]
        results = self._csv_results(user.id, cc_account.code, csv_rows)
        assert results[0]["status"] == "multiple"
        assert len(results[0]["matches"]) == 2

    def test_amount_mismatch(self, db, user, accounts, cc_account):
        """金額不一致 → unmatched"""
        make_journal(
            db, user.id,
            acct_debit_code="5010",
            acct_credit_code=cc_account.code,
            amount=999,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "不一致", "withdrawal": 1000, "deposit": 0},
        ]
        results = self._csv_results(user.id, cc_account.code, csv_rows)
        assert results[0]["status"] == "unmatched"

    def test_deposit_direction(self, db, user, accounts, cc_account):
        """入金方向の一致 — deposit → debit_amount でマッチ"""
        # CC口座へのデビット = 返金（CC残高が減る）
        make_journal(
            db, user.id,
            acct_debit_code=cc_account.code,           # CC にデビット（返金）
            acct_credit_code="5010",     # 食費を取消
            amount=500,
            entry_date=date(2026, 1, 10),
            source="journal",
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "返金", "withdrawal": 0, "deposit": 500},
        ]
        results = self._csv_results(user.id, cc_account.code, csv_rows)
        assert results[0]["status"] == "matched"
        assert results[0]["matches"][0]["amount"] == 500

    def test_no_date_in_csv_row(self, db, user, accounts, cc_account):
        """CSV行に日付なし → unmatched"""
        make_journal(
            db, user.id,
            acct_debit_code="5010",
            acct_credit_code=cc_account.code,
            amount=1000,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": None, "description": "日付なし", "withdrawal": 1000, "deposit": 0},
        ]
        results = self._csv_results(user.id, cc_account.code, csv_rows)
        assert results[0]["status"] == "unmatched"

    def test_no_duplicate_match(self, db, user, accounts, cc_account):
        """1仕訳が複数CSV行に重複マッチしない — 先にマッチした行が優先"""
        make_journal(
            db, user.id,
            acct_debit_code="5010",
            acct_credit_code=cc_account.code,
            amount=800,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "1行目", "withdrawal": 800, "deposit": 0},
            {"date": "2026-01-10", "description": "2行目", "withdrawal": 800, "deposit": 0},
        ]
        results = self._csv_results(user.id, cc_account.code, csv_rows)
        assert results[0]["status"] == "matched"
        assert results[1]["status"] == "unmatched"

    def test_empty_csv_rows(self, db, user, accounts, cc_account):
        """空のCSV行リスト → 空の結果"""
        result = find_matches(user.id, cc_account.code, [])
        assert result["csv_results"] == []
        assert result["journal_only"] == []
        assert result["daily_summary"] == []

    def test_zero_amount_row(self, db, user, accounts, cc_account):
        """入出金ゼロ → unmatched"""
        csv_rows = [
            {"date": "2026-01-10", "description": "ゼロ", "withdrawal": 0, "deposit": 0},
        ]
        results = self._csv_results(user.id, cc_account.code, csv_rows)
        assert results[0]["status"] == "unmatched"

    def test_multiple_same_day_same_amount(self, db, user, accounts, cc_account):
        """同日同金額の複数仕訳は全て候補に含まれる"""
        for desc in ("仕訳A", "仕訳B"):
            make_journal(
                db, user.id,
                acct_debit_code="5010",
                acct_credit_code=cc_account.code,
                amount=1200,
                entry_date=date(2026, 1, 10),
                description=desc,
            )
        csv_rows = [
            {"date": "2026-01-10", "description": "テスト", "withdrawal": 1200, "deposit": 0},
        ]
        results = self._csv_results(user.id, cc_account.code, csv_rows)
        assert results[0]["status"] == "multiple"
        assert len(results[0]["matches"]) == 2

    def test_other_user_entries_not_matched(self, db, user, accounts, cc_account,
                                            second_user, second_user_accounts):
        """他ユーザーの仕訳はマッチしない"""
        make_journal(
            db, second_user.id,
            acct_debit_code="5010",
            acct_credit_code="1010",
            amount=1000,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "他人", "withdrawal": 1000, "deposit": 0},
        ]
        results = self._csv_results(user.id, cc_account.code, csv_rows)
        assert results[0]["status"] == "unmatched"


class TestReturnStructure:
    """find_matches() の返り値構造テスト"""

    def test_returns_dict_with_keys(self, db, user, accounts, cc_account):
        """返り値が csv_results, journal_only, daily_summary を持つ dict"""
        csv_rows = [
            {"date": "2026-01-10", "description": "テスト", "withdrawal": 500, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        assert "csv_results" in result
        assert "journal_only" in result
        assert "daily_summary" in result


class TestJournalOnly:
    """journal_only（CSVにマッチしなかった仕訳）のテスト"""

    def test_unmatched_journal_detected(self, db, user, accounts, cc_account):
        """CSVにマッチしない仕訳が journal_only に含まれる"""
        make_journal(
            db, user.id,
            acct_debit_code="5010",
            acct_credit_code=cc_account.code,
            amount=1500,
            entry_date=date(2026, 1, 10),
            description="CSVにない仕訳",
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "別の取引", "withdrawal": 999, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        assert len(result["journal_only"]) == 1
        j = result["journal_only"][0]
        assert j["amount"] == 1500
        assert j["description"] == "CSVにない仕訳"

    def test_matched_journal_not_in_journal_only(self, db, user, accounts, cc_account):
        """マッチした仕訳は journal_only に含まれない"""
        make_journal(
            db, user.id,
            acct_debit_code="5010",
            acct_credit_code=cc_account.code,
            amount=1000,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "一致", "withdrawal": 1000, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        assert result["csv_results"][0]["status"] == "matched"
        assert len(result["journal_only"]) == 0

    def test_journal_only_excludes_multiple_candidates(self, db, user, accounts, cc_account):
        """multiple候補に含まれる仕訳も journal_only に含まれない"""
        for _ in range(2):
            make_journal(
                db, user.id,
                acct_debit_code="5010",
                acct_credit_code=cc_account.code,
                amount=1000,
                entry_date=date(2026, 1, 10),
            )
        csv_rows = [
            {"date": "2026-01-10", "description": "ランチ", "withdrawal": 1000, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        assert result["csv_results"][0]["status"] == "multiple"
        assert len(result["journal_only"]) == 0


class TestDailySummary:
    """daily_summary（日計サマリー）のテスト"""

    def test_matching_day_no_discrepancy(self, db, user, accounts, cc_account):
        """CSV1件・仕訳1件で金額一致 → 差異なし"""
        make_journal(
            db, user.id,
            acct_debit_code="5010",
            acct_credit_code=cc_account.code,
            amount=1000,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "テスト", "withdrawal": 1000, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        summary = result["daily_summary"]
        day = [s for s in summary if s["date"] == "2026-01-10"][0]
        assert day["csv_count"] == 1
        assert day["journal_count"] == 1
        assert day["diff_amount"] == 0
        assert day["has_discrepancy"] is False

    def test_csv_only_day(self, db, user, accounts, cc_account):
        """CSVにのみ存在する日 → 差異あり"""
        csv_rows = [
            {"date": "2026-01-10", "description": "新規", "withdrawal": 500, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        summary = result["daily_summary"]
        assert len(summary) == 1
        assert summary[0]["csv_count"] == 1
        assert summary[0]["journal_count"] == 0
        assert summary[0]["has_discrepancy"] is True

    def test_journal_only_day(self, db, user, accounts, cc_account):
        """仕訳にのみ存在する日 → 差異あり"""
        # CSV日付と同日に別金額の仕訳を作成（検索範囲に入る）
        make_journal(
            db, user.id,
            acct_debit_code="5010",
            acct_credit_code=cc_account.code,
            amount=800,
            entry_date=date(2026, 1, 10),
        )
        csv_rows = [
            {"date": "2026-01-10", "description": "別金額", "withdrawal": 999, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        summary = result["daily_summary"]
        day_10 = [s for s in summary if s["date"] == "2026-01-10"]
        assert len(day_10) == 1
        assert day_10[0]["journal_count"] == 1
        assert day_10[0]["csv_count"] == 1
        assert day_10[0]["has_discrepancy"] is True  # 金額差

    def test_same_day_count_discrepancy(self, db, user, accounts, cc_account):
        """同日同額でCSV3件・仕訳2件 → 件数差異を検出"""
        for _ in range(2):
            make_journal(
                db, user.id,
                acct_debit_code="5010",
                acct_credit_code=cc_account.code,
                amount=500,
                entry_date=date(2026, 1, 10),
            )
        csv_rows = [
            {"date": "2026-01-10", "description": "1件目", "withdrawal": 500, "deposit": 0},
            {"date": "2026-01-10", "description": "2件目", "withdrawal": 500, "deposit": 0},
            {"date": "2026-01-10", "description": "3件目", "withdrawal": 500, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        summary = result["daily_summary"]
        day = [s for s in summary if s["date"] == "2026-01-10"][0]
        assert day["csv_count"] == 3
        assert day["journal_count"] == 2
        assert day["diff_count"] == 1  # CSV が 1件多い
        assert day["diff_amount"] == 500  # ¥500 の過不足
        assert day["has_discrepancy"] is True

    def test_reverse_count_discrepancy(self, db, user, accounts, cc_account):
        """同日同額でCSV2件・仕訳3件 → 仕訳が多い差異を検出"""
        for _ in range(3):
            make_journal(
                db, user.id,
                acct_debit_code="5010",
                acct_credit_code=cc_account.code,
                amount=500,
                entry_date=date(2026, 1, 10),
            )
        csv_rows = [
            {"date": "2026-01-10", "description": "1件目", "withdrawal": 500, "deposit": 0},
            {"date": "2026-01-10", "description": "2件目", "withdrawal": 500, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        summary = result["daily_summary"]
        day = [s for s in summary if s["date"] == "2026-01-10"][0]
        assert day["csv_count"] == 2
        assert day["journal_count"] == 3
        assert day["diff_count"] == -1  # 仕訳が 1件多い
        assert day["diff_amount"] == -500
        assert day["has_discrepancy"] is True


class TestFindAiMatches:
    """AI照合のテスト"""

    def test_ai_matches_returns_results(self, db, user, accounts, cc_account):
        """AIが照合候補を返す"""
        mock_response = {"matches": [{"csv_index": 0, "entry_id": 99, "confidence": 0.8, "reason": "摘要類似"}]}
        unmatched = [{"csv_index": 0, "date": "2026-01-10", "description": "アマゾン", "amount": 1500}]
        journal = [{"entry_id": 99, "date": "2026-01-10", "description": "Amazon", "amount": 1480, "category_name": "日用品"}]

        with patch("app.services.ai_receipt._get_ai_config") as mock_config, \
             patch("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": lambda *a, **kw: mock_response}):
            mock_config.return_value = ("key", "openai", "gpt-4", None, "", {}, False)
            results = find_ai_matches(user.id, unmatched, journal)

        assert len(results) == 1
        assert results[0]["csv_index"] == 0
        assert results[0]["entry_id"] == 99
        assert results[0]["confidence"] == 0.8

    def test_ai_matches_filters_low_confidence(self, db, user, accounts):
        """confidence 0.3未満は除外される"""
        mock_response = {"matches": [{"csv_index": 0, "entry_id": 1, "confidence": 0.2, "reason": "低確信"}]}
        unmatched = [{"csv_index": 0, "date": "2026-01-10", "description": "何か", "amount": 500}]
        journal = [{"entry_id": 1, "date": "2026-01-10", "description": "別", "amount": 999, "category_name": "雑費"}]

        with patch("app.services.ai_receipt._get_ai_config") as mock_config, \
             patch("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": lambda *a, **kw: mock_response}):
            mock_config.return_value = ("key", "openai", "gpt-4", None, "", {}, False)
            results = find_ai_matches(user.id, unmatched, journal)

        assert len(results) == 0

    def test_ai_matches_empty_inputs(self, db, user, accounts):
        """入力が空の場合は空リストを返す"""
        with patch("app.services.ai_receipt._get_ai_config") as mock_config:
            mock_config.return_value = ("key", "openai", "gpt-4", None, "", {}, False)
            assert find_ai_matches(user.id, [], [{"entry_id": 1}]) == []
            assert find_ai_matches(user.id, [{"csv_index": 0}], []) == []


class TestEdgeCases:
    """エッジケースのテスト"""

    def test_csv_date_as_date_object(self, db, user, accounts, cc_account):
        """CSV行のdateがdateオブジェクト（文字列でない）でもマッチする"""
        make_journal(db, user.id, "5010", cc_account.code, 1000,
                     entry_date=date(2026, 1, 10))
        csv_rows = [
            {"date": date(2026, 1, 10), "description": "テスト", "withdrawal": 1000, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        assert result["csv_results"][0]["status"] == "matched"

    def test_csv_invalid_date_string(self, db, user, accounts, cc_account):
        """不正な日付文字列はunmatchedになる"""
        make_journal(db, user.id, "5010", cc_account.code, 1000,
                     entry_date=date(2026, 1, 10))
        csv_rows = [
            {"date": "invalid-date", "description": "テスト", "withdrawal": 1000, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        assert result["csv_results"][0]["status"] == "unmatched"

    def test_journal_only_deposit_direction(self, db, user, accounts, cc_account):
        """journal_onlyでdeposit方向（debit > 0）の仕訳も検出される"""
        make_journal(db, user.id, cc_account.code, "5010", 500,
                     entry_date=date(2026, 1, 10), source="journal")
        csv_rows = [
            {"date": "2026-01-10", "description": "別取引", "withdrawal": 9999, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        assert len(result["journal_only"]) == 1
        assert result["journal_only"][0]["direction"] == "deposit"

    def test_ai_non_dict_response_ignored(self, db, user, accounts):
        """text handlerがdict以外を返しても空リストを返す"""
        mock_response = "unexpected string"
        unmatched = [{"csv_index": 0, "date": "2026-01-10", "description": "テスト", "amount": 500}]
        journal = [{"entry_id": 1, "date": "2026-01-10", "description": "x", "amount": 499, "category_name": "雑"}]

        with patch("app.services.ai_receipt._get_ai_config") as mock_config, \
             patch("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": lambda *a, **kw: mock_response}):
            mock_config.return_value = ("key", "openai", "gpt-4", None, "", {}, False)
            results = find_ai_matches(user.id, unmatched, journal)
        assert results == []

    def test_all_dates_from_date_objects(self, db, user, accounts, cc_account):
        """CSV行のdateが全てdateオブジェクトでも日付範囲が正しく算出される"""
        make_journal(db, user.id, "5010", cc_account.code, 2000,
                     entry_date=date(2026, 2, 1))
        csv_rows = [
            {"date": date(2026, 2, 1), "description": "テスト", "withdrawal": 2000, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        assert result["csv_results"][0]["status"] == "matched"

    def test_all_dates_invalid_returns_all_unmatched(self, db, user, accounts, cc_account):
        """全CSV���の日付が不正 ��� 早期リターン"""
        csv_rows = [
            {"date": "invalid", "description": "1", "withdrawal": 1000, "deposit": 0},
            {"date": "bad", "description": "2", "withdrawal": 500, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        for r in result["csv_results"]:
            assert r["status"] == "unmatched"
        assert result["daily_summary"] == []

    def test_daily_summary_skips_invalid_dates(self, db, user, accounts, cc_account):
        """date=Noneや不正文字列のCSV行はサマリーから除外"""
        make_journal(db, user.id, "5010", cc_account.code, 1000,
                     entry_date=date(2026, 1, 10))
        csv_rows = [
            {"date": "2026-01-10", "description": "正常", "withdrawal": 1000, "deposit": 0},
            {"date": None, "description": "日付なし", "withdrawal": 500, "deposit": 0},
            {"date": "invalid", "description": "不正", "withdrawal": 300, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        day = [s for s in result["daily_summary"] if s["date"] == "2026-01-10"]
        assert len(day) == 1
        assert day[0]["csv_count"] == 1


class TestAiMatchesAdditional:
    """AI照合の追加テスト"""

    def test_unsupported_provider(self, db, user, accounts):
        """未対応プロバイダーはValueError"""
        unmatched = [{"csv_index": 0, "date": "2026-01-10", "description": "x", "amount": 500}]
        journal = [{"entry_id": 1, "date": "2026-01-10", "description": "y", "amount": 500, "category_name": "雑"}]
        with patch("app.services.ai_receipt._get_ai_config") as mock_config, \
             patch("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {}):
            mock_config.return_value = ("key", "unknown", "model", None, "", {}, False)
            with pytest.raises(ValueError, match="未対応"):
                find_ai_matches(user.id, unmatched, journal)

    def test_no_matches_key_in_response(self, db, user, accounts):
        """AI応答にmatchesキーがない場合は空リスト"""
        mock_response = {"error": "parse failed"}
        unmatched = [{"csv_index": 0, "date": "2026-01-10", "description": "x", "amount": 500}]
        journal = [{"entry_id": 1, "date": "2026-01-10", "description": "y", "amount": 500, "category_name": "雑"}]
        with patch("app.services.ai_receipt._get_ai_config") as mock_config, \
             patch("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": lambda *a, **kw: mock_response}):
            mock_config.return_value = ("key", "openai", "gpt-4", None, "", {}, False)
            results = find_ai_matches(user.id, unmatched, journal)
        assert results == []


class TestFormatHelpers:
    """_format_csv_rows / _format_journal_rows"""

    def test_format_csv_rows(self):
        rows = [
            {"csv_index": 0, "date": "2026-01-10", "description": "コンビニ", "amount": 1500},
            {"csv_index": 1, "date": "2026-01-11", "description": "スーパー", "amount": 3000},
        ]
        result = _format_csv_rows(rows)
        lines = result.split("\n")
        assert len(lines) == 2
        assert "[0]" in lines[0]
        assert "1,500" in lines[0]

    def test_format_csv_rows_missing_fields(self):
        rows = [{"csv_index": 0}]
        result = _format_csv_rows(rows)
        assert "[0]" in result

    def test_format_journal_rows(self):
        rows = [{"entry_id": 42, "date": "2026-01-10", "description": "Amazon",
                 "amount": 1480, "category_name": "日用品"}]
        result = _format_journal_rows(rows)
        assert "[ID:42]" in result
        assert "1,480" in result
        assert "(日用品)" in result

    def test_format_journal_rows_missing_fields(self):
        rows = [{"entry_id": 1}]
        result = _format_journal_rows(rows)
        assert "[ID:1]" in result
