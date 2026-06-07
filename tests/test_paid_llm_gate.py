"""自家ホスト LLM (llama.cpp) の有償ゲートのテスト (Phase 2 #67)。

無償ユーザーは BYOK 経由の外部プロバイダ (openai/anthropic/google) を
引き続き利用可能。サーバー提供 LLM (`llama_cpp`) は `paid_llm`
エンタイトルメントが必須。

セルフホストモード (`BILLING_BACKEND=unlimited`, デフォルト) では
`UnlimitedBillingClient` が全機能 True を返すため llama_cpp も使える。
"""

import pytest

from app.models.ai_config import UserAIConfig
from app.services.ai_receipt import _get_ai_config, encrypt_api_key
from app.services.entitlement import UnlimitedBillingClient


def _llama_cpp_config(db, user_id):
    cfg = UserAIConfig(
        user_id=user_id, provider="llama_cpp",
        api_key_encrypted=encrypt_api_key("_"),
        model_name="default",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _openai_config(db, user_id):
    cfg = UserAIConfig(
        user_id=user_id, provider="openai",
        api_key_encrypted=encrypt_api_key("sk-test"),
        model_name="gpt-4o",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


class TestPaidLlmGateUnlimitedMode:
    """デフォルト (BILLING_BACKEND=unlimited) では llama_cpp が使える"""

    def test_llama_cpp_passes_with_unlimited(self, db, user):
        _llama_cpp_config(db, user.id)
        api_key, provider, _, _, _, extra = _get_ai_config(user.id)
        assert provider == "llama_cpp"
        assert api_key == ""
        assert extra["base_url"] == "http://test-llama-cpp:8080"


class TestPaidLlmGateDenied:
    """`paid_llm` エンタイトルメントなしでは llama_cpp は拒否される"""

    def test_llama_cpp_denied_without_entitlement(self, db, user, monkeypatch):
        class DenyAllClient(UnlimitedBillingClient):
            def has_entitlement(self, user, feature_key):
                return False

        from app.services import entitlement as ent
        monkeypatch.setattr(ent, "get_billing_client", lambda: DenyAllClient())

        _llama_cpp_config(db, user.id)
        with pytest.raises(ValueError, match="有償プラン"):
            _get_ai_config(user.id)

    def test_byok_openai_unaffected_without_entitlement(self, db, user, monkeypatch):
        """無償プラン (paid_llm なし) でも BYOK の openai は使える"""
        class DenyAllClient(UnlimitedBillingClient):
            def has_entitlement(self, user, feature_key):
                return False

        from app.services import entitlement as ent
        monkeypatch.setattr(ent, "get_billing_client", lambda: DenyAllClient())

        _openai_config(db, user.id)
        api_key, provider, _, _, _, _ = _get_ai_config(user.id)
        assert provider == "openai"
        assert api_key == "sk-test"


class TestPaidLlmGateConditional:
    """`has_entitlement` の判定ごとに通過 / 拒否が切り替わる"""

    def test_llama_cpp_only_paid_llm_is_checked(self, db, user, monkeypatch):
        """`paid_llm` だけ False を返すクライアントでも llama_cpp は拒否される。

        他の feature_key (audit_seat 等) には影響しないことの確認は別レイヤー。
        """
        calls = []

        class SelectiveDenyClient(UnlimitedBillingClient):
            def has_entitlement(self, user, feature_key):
                calls.append(feature_key)
                return feature_key != "paid_llm"

        from app.services import entitlement as ent
        monkeypatch.setattr(
            ent, "get_billing_client", lambda: SelectiveDenyClient()
        )

        _llama_cpp_config(db, user.id)
        with pytest.raises(ValueError, match="有償プラン"):
            _get_ai_config(user.id)
        assert "paid_llm" in calls
