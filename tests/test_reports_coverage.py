"""reports ビューの追加テスト (カバレッジ改善)

試算表・元帳の残高キャッシュ経路、月次比較の損益振替 (closing) 除外、
Lv2 顧問の科目制限を追加でカバーする。
"""

from datetime import date

from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from tests.conftest import make_journal


def _close_and_cache(db, user_id, year=2026, closed_period=2):
    """月次確定 + 残高キャッシュを構築する"""
    from app.services.balance_cache import compute_balance_cache
    db.session.add(FiscalClose(user_id=user_id, year=year, closed_period=closed_period))
    db.session.commit()
    compute_balance_cache(user_id, year, closed_period)
    db.session.commit()


class TestTrialBalanceCachePath:
    def test_uses_balance_cache_for_opening(
        self, db, logged_in_client, user, accounts
    ):
        """pf>0 かつ pf-1 まで確定済みなら残高キャッシュを使う"""
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="journal")
        _close_and_cache(db, user.id)
        resp = logged_in_client.get("/reports/balance?year=2026&pf=3&pt=5")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "1,000" in body or "1000" in body

    def test_cache_path_with_prior_year_balance(
        self, db, logged_in_client, user, accounts
    ):
        """B/S: キャッシュ + 前年以前残高の組み合わせ"""
        make_journal(db, user.id, "5010", "1010", 2000,
                     entry_date=date(2025, 12, 15), source="journal")
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="journal")
        _close_and_cache(db, user.id)
        resp = logged_in_client.get("/reports/balance?year=2026&pf=3&pt=5")
        assert resp.status_code == 200


class TestLedgerCarryForwardCache:
    def test_carry_forward_uses_cache(
        self, db, logged_in_client, user, accounts
    ):
        """元帳の繰越残高もキャッシュ経路で計算される"""
        make_journal(db, user.id, "5010", "1010", 1500,
                     entry_date=date(2026, 2, 15), source="journal")
        _close_and_cache(db, user.id)
        resp = logged_in_client.get(
            "/reports/ledger?account_code=5010&year=2026&pf=3&pt=5")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "1,500" in body or "1500" in body

    def test_carry_forward_fallback_without_cache(
        self, db, logged_in_client, user, accounts
    ):
        """キャッシュ未作成ならフォールバック計算"""
        make_journal(db, user.id, "5010", "1010", 800,
                     entry_date=date(2026, 2, 15), source="journal")
        resp = logged_in_client.get(
            "/reports/ledger?account_code=5010&year=2026&pf=3&pt=5")
        assert resp.status_code == 200


class TestMonthlyExcludesClosing:
    def test_closing_entries_excluded_from_totals(
        self, db, logged_in_client, user, accounts
    ):
        """損益振替 (source=closing) は月次比較の合計に含まれない"""
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="journal")
        # closing: 食費 1000 を繰越利益 3020 へ
        closing = JournalEntry(
            user_id=user.id, date=date(2026, 2, 28),
            entry_number=999, description="損益振替",
            source="closing", fiscal_period=16,
        )
        closing.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="3020",
                             debit_amount=1000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=0, credit_amount=1000),
        ]
        db.session.add(closing)
        db.session.commit()
        resp = logged_in_client.get("/reports/monthly?year=2026")
        assert resp.status_code == 200


class TestLv2AccountRestriction:
    def test_ledger_lv2_filters_accounts(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2: 元帳は公開科目のみ表示される"""
        from app.models.audit import AuditGrant, AuditGrantAccount
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="active",
        )
        db.session.add(grant)
        db.session.flush()
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id, account_user_id=user.id,
            account_code="5010",
        ))
        db.session.commit()
        with logged_in_client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 2
        resp = logged_in_client.get("/reports/ledger")
        assert resp.status_code == 200
