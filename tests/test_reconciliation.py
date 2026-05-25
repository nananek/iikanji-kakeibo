"""CSV明細照合（マッチング）サービスのテスト"""

from datetime import date
from unittest.mock import patch

import pytest

from tests.conftest import make_journal
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.accounting import get_next_entry_number
from app.services.reconciliation import (
    find_matches, _format_csv_rows, _format_journal_rows,
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

    def test_date_within_tolerance_matched(self, db, user, accounts, cc_account):
        """金額一致・日付 1 日差 → matched (warn band)。
        旧仕様 (TOLERANCE=0) では unmatched だったが、±7 日まで許容するようになった。"""
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
        assert results[0]["status"] == "matched"
        assert results[0]["matches"][0]["date_band"] == "warn"
        assert results[0]["matches"][0]["date_diff_days"] == -1

    def test_date_beyond_tolerance_unmatched(self, db, user, accounts, cc_account):
        """8 日差 (トレランス外) → unmatched"""
        make_journal(
            db, user.id,
            acct_debit_code="5010",
            acct_credit_code=cc_account.code,
            amount=2000,
            entry_date=date(2026, 1, 18),
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


# TestFindAiMatches は対応関数の削除に伴い削除済。
# 等価のクライアント側ロジック (バッチ処理 / confidence フィルタ /
# 空入力 / dict 以外応答処理) は
# tests/static/js/test_reconcile_orchestrator.mjs でカバー。


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
        """journal_only で deposit 方向の仕訳が検出される (CSV も deposit を含む場合)。

        v3.10.2 以降、journal_only は CSV と同方向の仕訳のみを対象とする。
        deposit 方向の仕訳を検出するには CSV にも deposit 行が含まれている必要がある。
        """
        make_journal(db, user.id, cc_account.code, "5010", 500,
                     entry_date=date(2026, 1, 10), source="journal")
        csv_rows = [
            {"date": "2026-01-10", "description": "別取引", "withdrawal": 0, "deposit": 9999},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        assert len(result["journal_only"]) == 1
        assert result["journal_only"][0]["direction"] == "deposit"

    # test_ai_non_dict_response_ignored は JS テスト
    # (test_reconcile_orchestrator.mjs::"filterMatches: 非dict で空") へ移行済。

    def test_all_dates_from_date_objects(self, db, user, accounts, cc_account):
        """CSV行のdateが全てdateオブジェクトでも日付範囲が正しく算出される"""
        make_journal(db, user.id, "5010", cc_account.code, 2000,
                     entry_date=date(2026, 2, 1))
        csv_rows = [
            {"date": date(2026, 2, 1), "description": "テスト", "withdrawal": 2000, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        assert result["csv_results"][0]["status"] == "matched"

    def test_multi_line_same_account_individual_match(self, db, user, accounts, cc_account):
        """同一仕訳の同一口座複数行が個別にCSV行とマッチする"""
        entry = JournalEntry(
            user_id=user.id, date=date(2026, 1, 10),
            entry_number=get_next_entry_number(user.id),
            description="ゲーム課金", source="cashbook",
        )
        db.session.add(entry)
        db.session.flush()
        db.session.add(JournalEntryLine(
            journal_entry_id=entry.id, account_user_id=user.id,
            account_code="5010", debit_amount=24000, credit_amount=0,
        ))
        # CC行: ¥12,000 × 2行
        for _ in range(2):
            db.session.add(JournalEntryLine(
                journal_entry_id=entry.id, account_user_id=user.id,
                account_code=cc_account.code, debit_amount=0, credit_amount=12000,
            ))
        db.session.commit()

        csv_rows = [
            {"date": "2026-01-10", "description": "課金1",
             "withdrawal": 12000, "deposit": 0},
            {"date": "2026-01-10", "description": "課金2",
             "withdrawal": 12000, "deposit": 0},
        ]
        result = find_matches(user.id, cc_account.code, csv_rows)
        # 同一entry_idの2行がそれぞれCSV行とマッチ
        statuses = [r["status"] for r in result["csv_results"]]
        matched_count = statuses.count("matched") + statuses.count("multiple")
        assert matched_count == 2  # 両方マッチ（matched or multiple）

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


# TestAiMatchesAdditional は対応関数の削除に伴い削除済。
# 等価のクライアント側ロジックは test_reconcile_orchestrator.mjs でカバー。


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


# --- 日付トレランス±7日と段階バッジ ---


def _csv_row(d, amount, description="購入", direction="withdrawal"):
    return {
        "row_num": 1,
        "date": d if isinstance(d, str) else d.isoformat(),
        "description": description,
        "deposit": amount if direction == "deposit" else 0,
        "withdrawal": amount if direction == "withdrawal" else 0,
    }


class TestClassifyBand:
    """_classify_band() のバッジ境界値テスト"""

    def test_exact_at_zero(self):
        from app.services.reconciliation import _classify_band
        assert _classify_band(0) == "exact"

    def test_warn_lower_bound(self):
        from app.services.reconciliation import _classify_band
        assert _classify_band(1) == "warn"

    def test_warn_upper_bound(self):
        from app.services.reconciliation import _classify_band
        assert _classify_band(3) == "warn"

    def test_caution_lower_bound(self):
        from app.services.reconciliation import _classify_band
        assert _classify_band(4) == "caution"

    def test_caution_upper_bound(self):
        from app.services.reconciliation import _classify_band, MATCH_DATE_BAND_CAUTION
        assert _classify_band(MATCH_DATE_BAND_CAUTION) == "caution"


class TestDateTolerance:
    """日付トレランス拡張（±7日）と date_band / date_diff_days の挙動"""

    def test_constants_are_consistent(self):
        from app.services.reconciliation import (
            MATCH_DATE_TOLERANCE, MATCH_DATE_BAND_EXACT,
            MATCH_DATE_BAND_WARN, MATCH_DATE_BAND_CAUTION,
        )
        assert MATCH_DATE_TOLERANCE == MATCH_DATE_BAND_CAUTION == 7
        assert MATCH_DATE_BAND_EXACT == 0
        assert MATCH_DATE_BAND_WARN == 3

    def test_within_1day_returns_warn_band(self, db, user, accounts, cc_account):
        # 仕訳 1/10、CSV 1/11 (差 +1)
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-11", 1500)])
        match = result["csv_results"][0]["matches"][0]
        assert result["csv_results"][0]["status"] == "matched"
        assert match["date_diff_days"] == 1
        assert match["date_band"] == "warn"

    def test_within_minus_3days_returns_warn_band(self, db, user, accounts, cc_account):
        # 仕訳 1/13、CSV 1/10 (差 -3)
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 1, 13), source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-10", 1500)])
        match = result["csv_results"][0]["matches"][0]
        assert match["date_diff_days"] == -3
        assert match["date_band"] == "warn"

    def test_within_5days_returns_caution_band(self, db, user, accounts, cc_account):
        # 仕訳 1/10、CSV 1/15 (差 +5)
        make_journal(db, user.id, "5010", cc_account.code, 2000,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-15", 2000)])
        match = result["csv_results"][0]["matches"][0]
        assert match["date_diff_days"] == 5
        assert match["date_band"] == "caution"

    def test_exactly_7days_is_caution(self, db, user, accounts, cc_account):
        make_journal(db, user.id, "5010", cc_account.code, 1000,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-17", 1000)])
        match = result["csv_results"][0]["matches"][0]
        assert match["date_diff_days"] == 7
        assert match["date_band"] == "caution"

    def test_beyond_7days_unmatched(self, db, user, accounts, cc_account):
        """8 日差は照合対象外（仕訳取得クエリの範囲外なので journal_only にも出ない）。"""
        make_journal(db, user.id, "5010", cc_account.code, 1000,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-18", 1000)])
        assert result["csv_results"][0]["status"] == "unmatched"

    def test_exact_match_returns_zero_diff(self, db, user, accounts, cc_account):
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-10", 1500)])
        match = result["csv_results"][0]["matches"][0]
        assert match["date_diff_days"] == 0
        assert match["date_band"] == "exact"


class TestGreedyAssignment:
    """貪欲法による重複マッチ防止"""

    def test_prefers_exact_over_warn(self, db, user, accounts, cc_account):
        """CSV 2 行 (1/10, 1/12) と仕訳 1 件 (1/12, ¥1000) のとき、
        距離 0 (exact) の CSV 1/12 が優先で確定し、CSV 1/10 は unmatched になる。"""
        make_journal(db, user.id, "5010", cc_account.code, 1000,
                     entry_date=date(2026, 1, 12), source="ai_receipt")
        rows = [
            _csv_row("2026-01-10", 1000, description="CSV-10"),
            _csv_row("2026-01-12", 1000, description="CSV-12"),
        ]
        result = find_matches(user.id, cc_account.code, rows)
        statuses = [r["status"] for r in result["csv_results"]]
        assert statuses == ["unmatched", "matched"]
        assert result["csv_results"][1]["matches"][0]["date_band"] == "exact"

    def test_uses_closest_when_both_warn(self, db, user, accounts, cc_account):
        """CSV 1/10 に対し仕訳 1/11 (差 -1) と仕訳 1/13 (差 -3) が候補のとき、
        近い 1/11 を先頭に並べる (multiple 状態)。"""
        make_journal(db, user.id, "5010", cc_account.code, 2500,
                     entry_date=date(2026, 1, 11), source="ai_receipt",
                     description="近い仕訳")
        make_journal(db, user.id, "5010", cc_account.code, 2500,
                     entry_date=date(2026, 1, 13), source="ai_receipt",
                     description="遠い仕訳")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-10", 2500)])
        r = result["csv_results"][0]
        assert r["status"] == "multiple"
        assert r["matches"][0]["date"] == "2026-01-11"
        assert abs(r["matches"][0]["date_diff_days"]) == 1


class TestDailySummaryCrossDay:
    """日跨ぎマッチの日計サマリー集計"""

    def test_keeps_each_side_on_own_date(self, db, user, accounts, cc_account):
        """日跨ぎマッチでも CSV/仕訳の集計はそれぞれの本来日付に立つ。"""
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 1, 12), source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-10", 1500)])
        summary = {d["date"]: d for d in result["daily_summary"]}
        assert summary["2026-01-10"]["csv_count"] == 1
        assert summary["2026-01-10"]["journal_count"] == 0
        assert summary["2026-01-12"]["csv_count"] == 0
        assert summary["2026-01-12"]["journal_count"] == 1

    def test_cross_day_matched_counted_on_csv_date(self, db, user, accounts, cc_account):
        """cross_day_matched は CSV 日付側に集計される。"""
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 1, 12), source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-10", 1500)])
        summary = {d["date"]: d for d in result["daily_summary"]}
        assert summary["2026-01-10"]["cross_day_matched"] == 1
        assert summary["2026-01-12"]["cross_day_matched"] == 0

    def test_exact_match_no_cross_day_count(self, db, user, accounts, cc_account):
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-10", 1500)])
        summary = {d["date"]: d for d in result["daily_summary"]}
        assert summary["2026-01-10"]["cross_day_matched"] == 0


class TestPendingCardUnreceived:
    """「カード会社未達」(journal_only の経過日数情報・日計内訳) のテスト"""

    def test_journal_only_has_days_since_field(self, db, user, accounts, cc_account):
        """journal_only 各要素に days_since_journal が付く"""
        # 仕訳: 1/10 → CSV 1/10 別金額 → 仕訳は journal_only に入る
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row("2026-01-10", 9999)])
        assert len(result["journal_only"]) == 1
        j = result["journal_only"][0]
        assert "days_since_journal" in j
        assert isinstance(j["days_since_journal"], int)
        # is_stale フィールドも入る
        assert "is_stale" in j
        assert isinstance(j["is_stale"], bool)

    def test_journal_only_is_stale_when_over_30days(self, db, user, accounts,
                                                    cc_account, monkeypatch):
        """30 日超なら is_stale=True"""
        # 仕訳は 31 日前
        old_date = date(2026, 1, 10)
        today = date(2026, 2, 11)  # 32 日後
        import app.services.reconciliation as recon_mod

        class _FakeDate(date):
            @classmethod
            def today(cls):
                return today
        monkeypatch.setattr(recon_mod, "date", _FakeDate)

        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=old_date, source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row(old_date, 9999)])
        j = result["journal_only"][0]
        assert j["days_since_journal"] == 32
        assert j["is_stale"] is True

    def test_journal_only_not_stale_within_30days(self, db, user, accounts,
                                                  cc_account, monkeypatch):
        """30 日以内なら is_stale=False"""
        old_date = date(2026, 1, 10)
        today = date(2026, 1, 25)  # 15 日後
        import app.services.reconciliation as recon_mod

        class _FakeDate(date):
            @classmethod
            def today(cls):
                return today
        monkeypatch.setattr(recon_mod, "date", _FakeDate)

        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=old_date, source="ai_receipt")
        result = find_matches(user.id, cc_account.code, [_csv_row(old_date, 9999)])
        j = result["journal_only"][0]
        assert j["days_since_journal"] == 15
        assert j["is_stale"] is False

    def test_journal_only_sorted_by_days_desc(self, db, user, accounts, cc_account):
        """journal_only は経過日数の降順（古いものが先頭）。

        CSV 取込範囲を 1/5〜1/10 に揃え、3 件全てを範囲内に収める。
        """
        make_journal(db, user.id, "5010", cc_account.code, 100,
                     entry_date=date(2026, 1, 5), source="ai_receipt",
                     description="古い")
        make_journal(db, user.id, "5010", cc_account.code, 200,
                     entry_date=date(2026, 1, 10), source="ai_receipt",
                     description="新しい")
        make_journal(db, user.id, "5010", cc_account.code, 300,
                     entry_date=date(2026, 1, 8), source="ai_receipt",
                     description="中間")
        # CSV の min/max を 1/5〜1/10 にして 3 件を全て範囲内に入れる
        result = find_matches(user.id, cc_account.code, [
            _csv_row("2026-01-05", 9999),
            _csv_row("2026-01-10", 9998),
        ])
        assert len(result["journal_only"]) == 3
        descs = [j["description"] for j in result["journal_only"]]
        assert descs == ["古い", "中間", "新しい"]

    def test_daily_summary_has_pending_card_amount(self, db, user, accounts, cc_account):
        """日計サマリーに pending_card_amount が含まれ、その日の未達合計が立つ"""
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        # 別金額の CSV → 仕訳は journal_only に
        result = find_matches(user.id, cc_account.code,
                              [_csv_row("2026-01-10", 9999)])
        summary = {d["date"]: d for d in result["daily_summary"]}
        assert summary["2026-01-10"]["pending_card_amount"] == 1500

    def test_pending_card_amount_zero_when_all_matched(self, db, user, accounts,
                                                       cc_account):
        """全てマッチしていれば pending_card_amount は 0"""
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        result = find_matches(user.id, cc_account.code,
                              [_csv_row("2026-01-10", 1500)])
        summary = {d["date"]: d for d in result["daily_summary"]}
        assert summary["2026-01-10"]["pending_card_amount"] == 0


class TestJournalOnlyRangeLimit:
    """journal_only は CSV 取込範囲内の仕訳に限定される

    レシート起票仕訳が CSV 日付範囲外（過去）にあるとき、それは前回 CSV で
    既に照合済みのはず。今回の「カード会社未達」リストには含めない。
    """

    def test_excludes_journal_before_csv_min_date(self, db, user, accounts, cc_account):
        """CSV 範囲 (4/16〜5/15) の前にある仕訳 (4/10) は journal_only に含めない。

        トレランス ±7 日の取得範囲には入るが、未達としては報告しない。
        """
        make_journal(db, user.id, "5010", cc_account.code, 500,
                     entry_date=date(2026, 4, 10), source="ai_receipt",
                     description="範囲外（過去）")
        # CSV は 4/16 から
        result = find_matches(user.id, cc_account.code, [
            _csv_row("2026-04-16", 9999),
            _csv_row("2026-05-15", 8888),
        ])
        # 過去の仕訳は journal_only から除外される
        entry_ids = [j.get("entry_id") for j in result["journal_only"]]
        # 4/10 の仕訳は範囲外なので含まれない
        assert all(j["date"] >= "2026-04-16" for j in result["journal_only"])

    def test_excludes_journal_after_csv_max_date(self, db, user, accounts, cc_account):
        """CSV 範囲 (4/16〜5/15) の後にある仕訳 (5/20) は journal_only に含めない。"""
        make_journal(db, user.id, "5010", cc_account.code, 600,
                     entry_date=date(2026, 5, 20), source="ai_receipt",
                     description="範囲外（未来）")
        result = find_matches(user.id, cc_account.code, [
            _csv_row("2026-04-16", 9999),
            _csv_row("2026-05-15", 8888),
        ])
        assert all(j["date"] <= "2026-05-15" for j in result["journal_only"])

    def test_includes_journal_within_csv_range(self, db, user, accounts, cc_account):
        """CSV 範囲内（4/16〜5/15）の仕訳は別金額でも journal_only に含まれる。"""
        make_journal(db, user.id, "5010", cc_account.code, 700,
                     entry_date=date(2026, 5, 1), source="ai_receipt",
                     description="範囲内")
        result = find_matches(user.id, cc_account.code, [
            _csv_row("2026-04-16", 9999),
            _csv_row("2026-05-15", 8888),
        ])
        assert len(result["journal_only"]) == 1
        assert result["journal_only"][0]["description"] == "範囲内"

    def test_includes_journal_on_boundary(self, db, user, accounts, cc_account):
        """CSV 範囲の境界日 (min/max と同日) の仕訳は含まれる。"""
        make_journal(db, user.id, "5010", cc_account.code, 100,
                     entry_date=date(2026, 4, 16), source="ai_receipt",
                     description="min 境界")
        make_journal(db, user.id, "5010", cc_account.code, 200,
                     entry_date=date(2026, 5, 15), source="ai_receipt",
                     description="max 境界")
        result = find_matches(user.id, cc_account.code, [
            _csv_row("2026-04-16", 9999),
            _csv_row("2026-05-15", 8888),
        ])
        descs = sorted(j["description"] for j in result["journal_only"])
        assert descs == ["max 境界", "min 境界"]

    def test_matching_still_uses_tolerance(self, db, user, accounts, cc_account):
        """範囲制限は journal_only にだけ効き、マッチング自体は ±7 日トレランスで動く"""
        # 仕訳 4/14 (CSV min 4/16 の 2 日前)
        make_journal(db, user.id, "5010", cc_account.code, 1000,
                     entry_date=date(2026, 4, 14), source="ai_receipt")
        # CSV 4/16 (差 +2)
        result = find_matches(user.id, cc_account.code,
                              [_csv_row("2026-04-16", 1000)])
        # マッチング自体は成立（warn band）
        assert result["csv_results"][0]["status"] == "matched"
        assert result["csv_results"][0]["matches"][0]["date_band"] == "warn"
        # journal_only は空（マッチ済み）
        assert result["journal_only"] == []


class TestJournalOnlyDirectionFilter:
    """journal_only は CSV と同じ方向の仕訳のみを対象とする

    クレカ明細 CSV は出金 (withdrawal) のみが載るため、引き落とし仕訳
    (CC 未払金を銀行から支払う = CC 口座への deposit) は未達対象外。
    """

    def test_excludes_payoff_journal_when_csv_is_withdrawal_only(
        self, db, user, accounts, cc_account
    ):
        """出金のみの CSV のとき、引き落とし仕訳 (CC 口座へのdeposit) は除外"""
        # 通常の利用仕訳 (出金方向: 食費 / CC、CC は credit > 0)
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 4, 16), source="ai_receipt",
                     description="コンビニ")
        # 引き落とし仕訳 (CC / 普通預金、CC は debit > 0 = deposit 方向)
        make_journal(db, user.id, cc_account.code, "1020", 50000,
                     entry_date=date(2026, 4, 27), source="journal",
                     description="（カ）ジエーシービー 引き落とし")
        # CSV は出金行のみ
        result = find_matches(user.id, cc_account.code, [
            _csv_row("2026-04-16", 9999),  # 別金額で意図的に未マッチ
            _csv_row("2026-04-27", 8888),
        ])
        # journal_only には withdrawal 方向の仕訳のみ
        descriptions = [j["description"] for j in result["journal_only"]]
        assert "コンビニ" in descriptions
        assert "（カ）ジエーシービー 引き落とし" not in descriptions

    def test_excludes_refund_journal_when_csv_is_deposit_only(
        self, db, user, accounts, cc_account
    ):
        """入金のみの CSV のとき、通常の利用仕訳 (withdrawal 方向) は除外"""
        # 返金/入金 (CC 口座への debit = deposit 方向)
        make_journal(db, user.id, cc_account.code, "5010", 500,
                     entry_date=date(2026, 4, 16), source="ai_receipt",
                     description="返金")
        # 通常の支払い (withdrawal 方向)
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 4, 20), source="ai_receipt",
                     description="通常購入")
        # CSV は deposit のみ
        result = find_matches(user.id, cc_account.code, [
            {"row_num": 1, "date": "2026-04-16", "description": "返金",
             "deposit": 9999, "withdrawal": 0},
            {"row_num": 2, "date": "2026-04-20", "description": "別の入金",
             "deposit": 8888, "withdrawal": 0},
        ])
        descriptions = [j["description"] for j in result["journal_only"]]
        assert "返金" in descriptions
        assert "通常購入" not in descriptions

    def test_includes_both_directions_when_csv_has_both(
        self, db, user, accounts, cc_account
    ):
        """CSV に両方向が混在する場合は両方向の仕訳を対象とする"""
        make_journal(db, user.id, "5010", cc_account.code, 1500,
                     entry_date=date(2026, 4, 16), source="ai_receipt",
                     description="出金仕訳")
        make_journal(db, user.id, cc_account.code, "5010", 500,
                     entry_date=date(2026, 4, 20), source="ai_receipt",
                     description="入金仕訳")
        result = find_matches(user.id, cc_account.code, [
            _csv_row("2026-04-16", 9999),  # withdrawal
            {"row_num": 2, "date": "2026-04-20", "description": "返金",
             "deposit": 8888, "withdrawal": 0},  # deposit
        ])
        descriptions = [j["description"] for j in result["journal_only"]]
        assert "出金仕訳" in descriptions
        assert "入金仕訳" in descriptions
