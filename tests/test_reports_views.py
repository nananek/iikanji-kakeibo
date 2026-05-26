"""レポートビュー (reports.py) のテスト

各レポートページの GET をカバー。集計ロジックはクライアント側 JS の
test_*_view.mjs / test_*_renderer.mjs で網羅。
こちらはルート到達性とフィルタ動作を確認。
"""

from datetime import date

from tests.conftest import make_journal


class TestIndex:
    def test_unauthenticated(self, client):
        resp = client.get("/reports/")
        assert resp.status_code in (302, 401)


class TestBalance:
    def test_unauthenticated(self, client):
        resp = client.get("/reports/balance")
        assert resp.status_code in (302, 401)

    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/balance")
        assert resp.status_code == 200

    def test_with_year(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/balance?year=2026")
        assert resp.status_code == 200

    def test_with_period(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/balance?year=2026&period=2")
        assert resp.status_code == 200

    def test_with_entries(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        resp = logged_in_client.get("/reports/balance?year=2026")
        assert resp.status_code == 200


class TestBs:
    def test_unauthenticated(self, client):
        resp = client.get("/reports/bs")
        assert resp.status_code in (302, 401)

    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/bs")
        assert resp.status_code == 200

    def test_with_year(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/bs?year=2026")
        assert resp.status_code == 200

    def test_with_data(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "1020", "1010", 5000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        resp = logged_in_client.get("/reports/bs?year=2026")
        assert resp.status_code == 200


class TestPl:
    def test_unauthenticated(self, client):
        resp = client.get("/reports/pl")
        assert resp.status_code in (302, 401)

    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/pl")
        assert resp.status_code == 200

    def test_with_year(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/pl?year=2026")
        assert resp.status_code == 200

    def test_with_data(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        make_journal(db, user.id, "1010", "4010", 5000,
                     entry_date=date(2026, 2, 16), source="cashbook")
        resp = logged_in_client.get("/reports/pl?year=2026")
        assert resp.status_code == 200


class TestTax:
    def test_unauthenticated(self, client):
        resp = client.get("/reports/tax")
        assert resp.status_code in (302, 401)

    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/tax")
        assert resp.status_code == 200

    def test_with_year(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/tax?year=2026")
        assert resp.status_code == 200


# /reports/tax/medical-csv は Phase E3-F-4c で撤去。CSV 生成は
# `tests/static/js/test_medical_csv.mjs` でクライアント側を検証する。


class TestLedger:
    def test_unauthenticated(self, client):
        resp = client.get("/reports/ledger")
        assert resp.status_code in (302, 401)

    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/ledger")
        assert resp.status_code == 200

    def test_with_account(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/ledger?account_code=1010")
        assert resp.status_code == 200

    def test_with_account_and_year(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/ledger?account_code=1010&year=2026")
        assert resp.status_code == 200

    def test_with_data(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        resp = logged_in_client.get("/reports/ledger?account_code=1010&year=2026")
        assert resp.status_code == 200


class TestTaxFormReport:
    def test_unauthenticated(self, client):
        resp = client.get("/reports/tax-form")
        assert resp.status_code in (302, 401)

    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/tax-form")
        assert resp.status_code == 200

    def test_with_form_type(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/tax-form?form_type=real_estate")
        assert resp.status_code == 200


class TestMonthly:
    def test_unauthenticated(self, client):
        resp = client.get("/reports/monthly")
        assert resp.status_code in (302, 401)

    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/monthly")
        assert resp.status_code == 200

    def test_with_year(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/monthly?year=2026")
        assert resp.status_code == 200

    def test_with_data(self, db, logged_in_client, user, accounts):
        for m in range(1, 13):
            make_journal(db, user.id, "5010", "1010", 100 * m,
                         entry_date=date(2026, m, 15), source="cashbook")
        resp = logged_in_client.get("/reports/monthly?year=2026")
        assert resp.status_code == 200
