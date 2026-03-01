"""月次比較レポート — 区分分析グラフの出力テスト"""

from datetime import date

import pytest

from app.models.account import Account
from tests.conftest import make_journal


@pytest.fixture
def logged_in_client(app, client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return client


class TestIncomeTypeChart:
    """収入区分分析の canvas が正しく出力される"""

    def test_no_income_hides_chart(self, db, logged_in_client, accounts):
        """収入がなければ incomeTypeChart は出力されない"""
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert "incomeTypeChart" not in html

    def test_income_shows_canvas(self, db, logged_in_client, accounts):
        """収入があれば incomeTypeChart canvas が出力される"""
        make_journal(db, accounts["4010"].user_id,
                     "1020", "4010",
                     300000, entry_date=date(2026, 1, 25))
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert 'id="incomeTypeChart"' in html
        assert 'id="incomeTypeTrendChart"' in html

    def test_income_chart_js_initialized(self, db, logged_in_client, accounts):
        """Chart.js の初期化コードが出力される"""
        make_journal(db, accounts["4010"].user_id,
                     "1020", "4010",
                     300000, entry_date=date(2026, 1, 25))
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert "incomeTypeChart" in html
        assert "incomeTypeTrendChart" in html
        assert "buildMonthlyData" in html

    def test_income_cost_type_in_json(self, db, logged_in_client, accounts):
        """income_accounts の tojson 出力に cost_type が含まれる"""
        accounts["4010"].cost_type = "fixed"
        db.session.commit()
        make_journal(db, accounts["4010"].user_id,
                     "1020", "4010",
                     300000, entry_date=date(2026, 1, 25))
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert '"cost_type": "fixed"' in html

    def test_income_cost_type_null_in_json(self, db, logged_in_client, accounts):
        """cost_type 未設定の場合 null が出力される"""
        make_journal(db, accounts["4010"].user_id,
                     "1020", "4010",
                     300000, entry_date=date(2026, 1, 25))
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert '"cost_type": null' in html

    def test_income_breakdown_table(self, db, logged_in_client, accounts):
        """固定収入・変動収入・臨時収入の内訳表が出力される"""
        accounts["4010"].cost_type = "fixed"
        db.session.commit()
        make_journal(db, accounts["4010"].user_id,
                     "1020", "4010",
                     300000, entry_date=date(2026, 1, 25))
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert "固定収入" in html
        assert "変動収入" in html
        assert "臨時収入" in html
        assert "300,000" in html


class TestExpenseTypeChart:
    """支出区分分析の canvas テスト（対照用）"""

    def test_no_expense_hides_chart(self, db, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert "costTypeChart" not in html

    def test_expense_shows_canvas(self, db, logged_in_client, accounts):
        make_journal(db, accounts["5010"].user_id,
                     "5010", "1010",
                     5000, entry_date=date(2026, 1, 15))
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert 'id="costTypeChart"' in html
        assert 'id="costTypeTrendChart"' in html
