"""月次比較レポート — 区分分析グラフの出力テスト

E3-F-3d 以降、月次比較はクライアント描画。サーバ HTML には canvas が
常時含まれ d-none で表示制御されるため、テストは accounts_meta JSON 内の
cost_type / is_business を検証する。
"""

import json
import re
from datetime import date

import pytest

from app.models.account import Account
from tests.conftest import make_journal


@pytest.fixture
def logged_in_client(app, client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return client


def _parse_meta(html):
    m = re.search(
        r'<script id="monthly-accounts-meta"[^>]*>(.*?)</script>',
        html, flags=re.DOTALL,
    )
    assert m, "monthly-accounts-meta script not found"
    return json.loads(m.group(1).strip())


class TestIncomeTypeChart:
    """収入区分分析: accounts_meta JSON に必要な cost_type が含まれる。
    canvas 要素自体は常に DOM に存在し、表示は client が d-none を toggle する。"""

    def test_canvas_always_present(self, db, logged_in_client, accounts):
        """canvas は HTML に常時含まれる (renderer が d-none を切り替える)"""
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert 'id="incomeTypeChart"' in html
        assert 'id="incomeTypeTrendChart"' in html

    def test_renderer_module_loaded(self, db, logged_in_client, accounts):
        """monthly_comparison_renderer.mjs が script として読み込まれる"""
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert "monthly_comparison_renderer.mjs" in html

    def test_income_cost_type_in_meta(self, db, logged_in_client, accounts):
        """cost_type=fixed 設定が accounts_meta に反映される"""
        accounts["4010"].cost_type = "fixed"
        db.session.commit()
        resp = logged_in_client.get("/reports/monthly?year=2026")
        meta = _parse_meta(resp.data.decode())
        assert meta["4010"]["cost_type"] == "fixed"

    def test_income_cost_type_default_occasional(self, db, logged_in_client, accounts):
        """cost_type 未設定なら 'occasional' を default で埋める"""
        resp = logged_in_client.get("/reports/monthly?year=2026")
        meta = _parse_meta(resp.data.decode())
        # accounts["4010"] は cost_type 未設定 (None)
        assert meta["4010"]["cost_type"] == "occasional"

    def test_breakdown_labels_in_template(self, db, logged_in_client, accounts):
        """固定収入・変動収入・臨時収入のラベルは template に常時含まれる"""
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert "固定収入" in html
        assert "変動収入" in html
        assert "臨時収入" in html


class TestExpenseTypeChart:
    """支出区分分析の canvas テスト (対照用)"""

    def test_canvas_always_present(self, db, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert 'id="costTypeChart"' in html
        assert 'id="costTypeTrendChart"' in html
