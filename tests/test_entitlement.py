"""エンタイトルメント基盤 (`app/services/entitlement.py`) のテスト。

Phase 2 #67 の骨格部分:
- `UnlimitedBillingClient` が全機能を解放する
- `get_billing_client()` が `BILLING_BACKEND` に応じて切替
- `has_entitlement` / `get_auditor_capacity` / `get_entitlement_summary`
- `require_entitlement` デコレータの認証/認可分岐
"""

import pytest
from werkzeug.exceptions import Forbidden, Unauthorized

from app.services.entitlement import (
    BillingClient,
    UnlimitedBillingClient,
    get_auditor_capacity,
    get_billing_client,
    get_entitlement_summary,
    has_entitlement,
    require_entitlement,
)


# --- UnlimitedBillingClient -------------------------------------------------


class TestUnlimitedBillingClient:
    """セルフホスト用クライアント: 全機能を True で返す"""

    def test_is_subclass_of_billing_client(self):
        assert issubclass(UnlimitedBillingClient, BillingClient)

    @pytest.mark.parametrize(
        "feature_key",
        [
            "paid_llm",
            "voucher_storage",
            "timestamp_seal",
            "audit_seat",
            "auditor_plan_small",
            "auditor_plan_medium",
            "auditor_plan_unlimited",
        ],
    )
    def test_has_entitlement_always_true(self, feature_key):
        client = UnlimitedBillingClient()
        assert client.has_entitlement(user=None, feature_key=feature_key) is True

    def test_auditor_capacity_is_unlimited(self):
        client = UnlimitedBillingClient()
        assert client.get_auditor_capacity(user=None) is None

    def test_summary_shape(self):
        client = UnlimitedBillingClient()
        summary = client.get_summary(user=None)
        assert summary == {
            "mode": "unlimited",
            "all_features_enabled": True,
            "auditor_capacity": None,
        }


# --- ファクトリ -------------------------------------------------------------


class TestGetBillingClient:
    """`get_billing_client()` の挙動"""

    def test_default_is_unlimited(self, app):
        with app.app_context():
            client = get_billing_client()
            assert isinstance(client, UnlimitedBillingClient)

    def test_explicit_unlimited(self, app, monkeypatch):
        monkeypatch.setitem(app.config, "BILLING_BACKEND", "unlimited")
        with app.app_context():
            client = get_billing_client()
            assert isinstance(client, UnlimitedBillingClient)

    def test_http_backend_raises_not_implemented(self, app, monkeypatch):
        monkeypatch.setitem(app.config, "BILLING_BACKEND", "http")
        with app.app_context():
            with pytest.raises(NotImplementedError):
                get_billing_client()

    def test_unknown_backend_raises_runtime_error(self, app, monkeypatch):
        monkeypatch.setitem(app.config, "BILLING_BACKEND", "bogus")
        with app.app_context():
            with pytest.raises(RuntimeError, match="未知の BILLING_BACKEND"):
                get_billing_client()


# --- トップレベル関数 -------------------------------------------------------


class TestTopLevelHelpers:
    """`has_entitlement` / `get_auditor_capacity` / `get_entitlement_summary`"""

    def test_has_entitlement_unlimited(self, app, user):
        with app.app_context():
            assert has_entitlement(user, "paid_llm") is True

    def test_get_auditor_capacity_unlimited(self, app, user):
        with app.app_context():
            assert get_auditor_capacity(user) is None

    def test_summary_unlimited(self, app, user):
        with app.app_context():
            summary = get_entitlement_summary(user)
            assert summary["mode"] == "unlimited"
            assert summary["all_features_enabled"] is True


# --- require_entitlement デコレータ ----------------------------------------


class TestRequireEntitlement:
    """デコレータを直接適用した関数を呼んで挙動を確認する。

    blueprint 登録は session app の制約で後付けできないため、
    `test_request_context` 内で abort 例外を直接捕まえる方式にしている。
    """

    def test_anonymous_raises_unauthorized(self, app):
        @require_entitlement("paid_llm")
        def view():
            return "ok"

        with app.test_request_context():
            with pytest.raises(Unauthorized):
                view()

    def test_authenticated_with_unlimited_passes(self, app, user):
        from flask_login import login_user

        @require_entitlement("paid_llm")
        def view():
            return "ok"

        with app.test_request_context():
            login_user(user)
            assert view() == "ok"

    def test_denied_when_billing_client_returns_false(self, app, user, monkeypatch):
        """`has_entitlement` を False に固定すると Forbidden。"""
        from flask_login import login_user

        class DenyAllClient(UnlimitedBillingClient):
            def has_entitlement(self, user, feature_key):
                return False

        from app.services import entitlement as ent

        monkeypatch.setattr(ent, "get_billing_client", lambda: DenyAllClient())

        @require_entitlement("paid_llm")
        def view():
            return "ok"

        with app.test_request_context():
            login_user(user)
            with pytest.raises(Forbidden):
                view()


# --- インターフェース整合 ---------------------------------------------------


class TestBillingClientAbstract:
    """`BillingClient` 抽象クラスは直接インスタンス化できない"""

    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            BillingClient()
