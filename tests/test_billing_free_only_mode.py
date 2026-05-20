"""BILLING_BACKEND=free_only モードのテスト (Phase 2 #67 拡張)."""

import pytest

from app.services.entitlement import (
    FreeOnlyBillingClient,
    UnlimitedBillingClient,
    get_billing_client,
    has_entitlement,
)


class TestFreeOnlyBillingClient:
    """`FreeOnlyBillingClient` 単体の挙動."""

    def test_has_entitlement_all_false(self, user):
        # FreeOnlyBillingClient はステートレスで current_app を参照しない
        # ため app fixture 不要
        client = FreeOnlyBillingClient()
        for key in [
            "paid_llm",
            "voucher_storage",
            "timestamp_seal",
            "audit_seat",
            "auditor_plan_small",
            "auditor_plan_medium",
            "auditor_plan_unlimited",
        ]:
            assert client.has_entitlement(user, key) is False, key

    def test_get_auditor_capacity_returns_zero(self, user):
        client = FreeOnlyBillingClient()
        assert client.get_auditor_capacity(user) == 0

    def test_get_entitlement_summary(self, user):
        client = FreeOnlyBillingClient()
        summary = client.get_entitlement_summary(user)
        assert summary["mode"] == "free_only"
        assert summary["all_features_enabled"] is False
        assert summary["auditor_capacity"] == 0


class TestGetBillingClientFactory:
    """`get_billing_client()` の 3 モード切替."""

    def test_default_unlimited(self, app):
        with app.app_context():
            app.config["BILLING_BACKEND"] = "unlimited"
            assert isinstance(get_billing_client(), UnlimitedBillingClient)

    def test_free_only_selected(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "BILLING_BACKEND", "free_only")
            client = get_billing_client()
            assert isinstance(client, FreeOnlyBillingClient)

    def test_http_still_not_implemented(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "BILLING_BACKEND", "http")
            with pytest.raises(NotImplementedError):
                get_billing_client()

    def test_unknown_raises_runtime_error(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "BILLING_BACKEND", "wat")
            with pytest.raises(RuntimeError):
                get_billing_client()


class TestFreeOnlyIntegrationStorage:
    """`free_only` モードで voucher_storage が拒否される動作確認."""

    def test_has_entitlement_voucher_storage_false(
        self, app, user, monkeypatch,
    ):
        with app.app_context():
            monkeypatch.setitem(app.config, "BILLING_BACKEND", "free_only")
            assert has_entitlement(user, "voucher_storage") is False

    def test_storage_summary_returns_none_in_free_only(
        self, logged_in_client, app, user, monkeypatch,
    ):
        """free_only モードでは設定画面のストレージセクションが非表示."""
        monkeypatch.setitem(app.config, "BILLING_BACKEND", "free_only")
        resp = logged_in_client.get("/settings/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # ストレージ使用量セクションが出ない
        assert "ストレージ使用量" not in body
        # 「無償機能のみ」プラン表示が出る
        assert "無償機能のみ" in body


class TestFreeOnlyTemplateRendering:
    """settings/index.html の `free_only` モード表示."""

    def test_free_only_panel_visible(
        self, logged_in_client, app, monkeypatch,
    ):
        monkeypatch.setitem(app.config, "BILLING_BACKEND", "free_only")
        resp = logged_in_client.get("/settings/")
        body = resp.get_data(as_text=True)
        assert "無償機能のみ" in body
        # 全機能解放モードの文言は出ない
        assert "全機能解放" not in body

    def test_unlimited_panel_visible(
        self, logged_in_client, app, monkeypatch,
    ):
        monkeypatch.setitem(app.config, "BILLING_BACKEND", "unlimited")
        resp = logged_in_client.get("/settings/")
        body = resp.get_data(as_text=True)
        assert "全機能解放" in body
        # 無償機能のみモードの文言は出ない
        assert "無償機能のみ" not in body
