"""/api/v1/reports/* のテスト + services/reports.py 単体テスト"""

from datetime import date

import pytest

from app.models.api_key import APIKey
from app.models.balance_cache import BalanceCache
from app.models.fiscal import FiscalClose
from app.models.oauth import OAuthToken
from app.services.balance_cache import compute_balance_cache
from app.services.reports import (
    compute_income_statement, compute_trial_balance,
)
from tests.conftest import _auth_header, make_journal


def _ro_token(db, user):
    raw, h, prefix = OAuthToken.generate()
    token = OAuthToken(
        user_id=user.id, name="ro", token_hash=h, token_prefix=prefix,
        read_only=True,
    )
    db.session.add(token)
    db.session.commit()
    return raw


# --- 試算表 ---


class TestTrialBalance:
    def test_unauthenticated(self, client):
        resp = client.get("/api/v1/reports/trial-balance")
        assert resp.status_code == 401

    def test_apikey_without_reports_scope_rejected(self, client, db, user, accounts):
        raw, h, prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="no-reports",
            key_hash=h, key_prefix=prefix,
            scopes="journals:read", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.get(
            "/api/v1/reports/trial-balance",
            headers=_auth_header(raw),
        )
        assert resp.status_code == 403

    def test_returns_balances(self, client, db, user, auth_header, accounts):
        make_journal(db, user.id, "5010", "1010", 3000, entry_date=date(2026, 5, 15))
        make_journal(db, user.id, "5010", "1010", 1000, entry_date=date(2026, 5, 20))
        resp = client.get(
            "/api/v1/reports/trial-balance?year=2026",
            headers=auth_header,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["year"] == 2026
        codes = {b["account_code"] for b in body["balances"]}
        assert "5010" in codes
        assert "1010" in codes
        # 5010 (expense, debit normal) は 借方 4000
        item_5010 = next(b for b in body["balances"] if b["account_code"] == "5010")
        assert item_5010["debit"] == 4000
        assert item_5010["balance"] == 4000

    def test_period_range_filter(self, client, db, user, auth_header, accounts):
        make_journal(db, user.id, "5010", "1010", 1000, entry_date=date(2026, 3, 15))
        make_journal(db, user.id, "5010", "1010", 2000, entry_date=date(2026, 6, 15))
        resp = client.get(
            "/api/v1/reports/trial-balance?year=2026&period_from=6&period_to=6",
            headers=auth_header,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        item_5010 = next(b for b in body["balances"] if b["account_code"] == "5010")
        # 6月分のみ
        assert item_5010["debit"] == 2000

    def test_oauth_readonly_token_can_access(self, client, db, user, accounts):
        raw = _ro_token(db, user)
        resp = client.get(
            "/api/v1/reports/trial-balance",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200


# --- 損益計算書 ---


class TestIncomeStatement:
    def test_year_summary(self, client, db, user, auth_header, accounts):
        # 給与 4010 で収入 100,000
        make_journal(db, user.id, "1010", "4010", 100000, entry_date=date(2026, 3, 15))
        # 食費 5010 で支出 30,000
        make_journal(db, user.id, "5010", "1010", 30000, entry_date=date(2026, 4, 15))
        resp = client.get(
            "/api/v1/reports/income-statement?year=2026",
            headers=auth_header,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["income_total"] == 100000
        assert body["expense_total"] == 30000
        assert body["net_income"] == 70000
        assert body["month"] is None
        assert any(b["account_code"] == "4010" for b in body["income_breakdown"])
        assert any(b["account_code"] == "5010" for b in body["expense_breakdown"])

    def test_month_filter(self, client, db, user, auth_header, accounts):
        make_journal(db, user.id, "5010", "1010", 1000, entry_date=date(2026, 3, 15))
        make_journal(db, user.id, "5010", "1010", 2000, entry_date=date(2026, 6, 15))
        resp = client.get(
            "/api/v1/reports/income-statement?year=2026&month=6",
            headers=auth_header,
        )
        body = resp.get_json()
        assert body["month"] == 6
        assert body["expense_total"] == 2000

    def test_invalid_month(self, client, db, user, auth_header, accounts):
        resp = client.get(
            "/api/v1/reports/income-statement?year=2026&month=13",
            headers=auth_header,
        )
        assert resp.status_code == 400

    def test_default_year(self, client, db, user, auth_header, accounts):
        resp = client.get(
            "/api/v1/reports/income-statement",
            headers=auth_header,
        )
        assert resp.status_code == 200
        assert resp.get_json()["year"] == date.today().year


# --- 月次比較 ---


class TestMonthlyComparison:
    def test_returns_per_month_totals(self, client, db, user, auth_header, accounts):
        make_journal(db, user.id, "5010", "1010", 1000, entry_date=date(2026, 1, 5))
        make_journal(db, user.id, "5010", "1010", 2000, entry_date=date(2026, 6, 10))
        resp = client.get(
            "/api/v1/reports/monthly?year=2026",
            headers=auth_header,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["year"] == 2026
        # 1月 + 6月の支出
        assert body["expense_totals"][0] == 1000
        assert body["expense_totals"][5] == 2000
        # 全要素 int
        assert all(isinstance(v, int) for v in body["expense_totals"])
        assert all(isinstance(v, int) for v in body["income_totals"])

    def test_oauth_readonly_token_can_access(self, client, db, user, accounts):
        raw = _ro_token(db, user)
        resp = client.get(
            "/api/v1/reports/monthly",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200


# --- 税務集計 ---


class TestTaxReport:
    def test_returns_tax_and_medical(self, client, db, user, auth_header, accounts):
        resp = client.get(
            "/api/v1/reports/tax?year=2026",
            headers=auth_header,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["year"] == 2026
        assert "tax_summary" in body
        assert "medical_summary" in body
        # JSON serializable (no Decimal)
        import json
        json.dumps(body)

    def test_user_isolation(self, client, db, user, second_user, accounts, second_user_accounts, auth_header):
        # 別ユーザーの仕訳は見えない
        make_journal(db, second_user.id, "5010", "1010", 99999, entry_date=date(2026, 3, 15))
        resp = client.get(
            "/api/v1/reports/tax?year=2026",
            headers=auth_header,
        )
        assert resp.status_code == 200
        # user 自身の data のみ — 99999 が含まれないことの間接確認
        body = resp.get_json()
        for cat_data in body["tax_summary"].values():
            assert cat_data["total"] != 99999


# --- services/reports.py 単体テスト ---


class TestComputeTrialBalanceCacheHit:
    """確定済み残高キャッシュを使うパスのテスト"""

    def _seed_for_cache(self, db, user, accounts):
        """1月に仕訳作成 → 1月確定 + キャッシュ生成"""
        make_journal(db, user.id, "5010", "1010", 1000, entry_date=date(2026, 1, 10))
        make_journal(db, user.id, "1020", "4010", 50000, entry_date=date(2026, 1, 20))
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

    def test_cache_hit_for_pl_account(self, db, user, accounts):
        """P/L 科目: キャッシュから当年累計を取得して期首残高を計算"""
        self._seed_for_cache(db, user, accounts)
        # 2 月以降を見る (pf=2 → cache の period=1 を参照)
        balances = compute_trial_balance(user.id, 2026, pf=2, pt=12)
        item_5010 = next(b for b in balances if b["account_code"] == "5010")
        # 5010 (expense, debit normal): キャッシュから opening = 1000 (1月分)
        assert item_5010["opening"] == 1000

    def test_cache_hit_for_bs_account_debit_normal(self, db, user, accounts):
        """B/S 借方科目 (現金 1010): cache + 前年以前を合算"""
        self._seed_for_cache(db, user, accounts)
        balances = compute_trial_balance(user.id, 2026, pf=2, pt=12)
        item_1010 = next(b for b in balances if b["account_code"] == "1010")
        # 1010 (asset, debit normal): 借方 0, 貸方 1000 → 当年累計の opening = -1000
        assert item_1010["opening"] == -1000

    def test_cache_hit_for_bs_account_credit_normal(self, db, user, accounts):
        """B/S 貸方科目 (純資産): cache 路の credit-normal 分岐"""
        # 元入金 (credit normal) を 1月に発生させる
        make_journal(db, user.id, "1010", "3010", 200000, entry_date=date(2026, 1, 5))
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        compute_balance_cache(user.id, 2026, 1)
        db.session.commit()

        balances = compute_trial_balance(user.id, 2026, pf=2, pt=12)
        item_3010 = next(b for b in balances if b["account_code"] == "3010")
        # 3010 (equity, credit normal): credit 200000 - debit 0 = 200000
        assert item_3010["opening"] == 200000

    def test_no_cache_fallback_pf_gt_0(self, db, user, accounts):
        """確定がない (pf > 0 だがキャッシュ未生成): フォールバック計算"""
        # 1月に仕訳、確定なし
        make_journal(db, user.id, "5010", "1010", 800, entry_date=date(2026, 1, 10))
        # FiscalClose なし → use_cache=False, elif pf>0 ブランチへ
        balances = compute_trial_balance(user.id, 2026, pf=2, pt=12)
        item_5010 = next(b for b in balances if b["account_code"] == "5010")
        # 5010 expense debit normal、フォールバックで前期 (1月) を集計
        assert item_5010["opening"] == 800

    def test_no_cache_fallback_bs_account(self, db, user, accounts):
        """B/S 科目 (現金) の non-cache フォールバック路"""
        make_journal(db, user.id, "1010", "4010", 50000, entry_date=date(2026, 1, 5))
        balances = compute_trial_balance(user.id, 2026, pf=2, pt=12)
        item_1010 = next(b for b in balances if b["account_code"] == "1010")
        # 1010 asset debit normal: 1月の借方 50000 → opening = 50000
        assert item_1010["opening"] == 50000


class TestComputeIncomeStatementFallback:
    """compute_income_statement の特殊パス"""

    def test_no_account_types_returns_zero(self, db, user):
        """AccountType が存在しない場合は空サマリーを返す"""
        # account_types fixture を使わず、空状態
        result = compute_income_statement(user.id, 2026)
        assert result["income_total"] == 0
        assert result["expense_total"] == 0
        assert result["net_income"] == 0
        assert result["income_breakdown"] == []
        assert result["expense_breakdown"] == []
