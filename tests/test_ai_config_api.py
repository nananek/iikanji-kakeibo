"""E2EE 化された /api/v1/ai-config API のテスト (設計書 §11.5)。

Phase E2-b で Fernet 完全廃止に伴い migrate-key / CLI 関連テストは削除。
クライアント側 (Web ブラウザの AES-GCM 暗号化) は test_*_orchestrator.mjs で別途。
"""

import json
from base64 import b64decode, b64encode

import pytest

from app.extensions import db
from app.models.ai_config import UserAIConfig


def _ai_config(user_id, *, with_e2ee=True):
    """テスト用 UserAIConfig 生成ヘルパー。"""
    cfg = UserAIConfig(
        user_id=user_id,
        provider="openai",
        model_name="gpt-4o-mini",
        custom_prompt="",
        compliance_check=False,
    )
    if with_e2ee:
        cfg.api_key_blob = b"\xAA" * 48  # ダミー暗号文 (48B = 32B 鍵 + 16B タグ)
        cfg.api_key_iv = b"\xBB" * 12
    db.session.add(cfg)
    db.session.commit()
    return cfg


class TestGetAiConfig:
    """GET /api/v1/ai-config"""

    def test_unauthenticated(self, client):
        resp = client.get("/api/v1/ai-config")
        assert resp.status_code in (302, 401)

    def test_not_set_returns_404(self, logged_in_client, accounts):
        resp = logged_in_client.get("/api/v1/ai-config")
        assert resp.status_code == 404

    def test_returns_blob_and_iv_when_e2ee(
        self, db, logged_in_client, user, accounts,
    ):
        _ai_config(user.id, with_e2ee=True)
        resp = logged_in_client.get("/api/v1/ai-config")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["provider"] == "openai"
        assert body["is_e2ee"] is True
        # base64 デコードして元のバイト列が返ってきていること
        assert b64decode(body["api_key_blob"]) == b"\xAA" * 48
        assert b64decode(body["api_key_iv"]) == b"\xBB" * 12

    def test_blob_iv_null_when_no_e2ee_data(
        self, db, logged_in_client, user, accounts,
    ):
        # blob/iv 未設定 (provider のみ登録) — 通常 PUT が常に blob/iv を
        # セットするので発生しない、防御的テスト。
        _ai_config(user.id, with_e2ee=False)
        resp = logged_in_client.get("/api/v1/ai-config")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["is_e2ee"] is False
        assert body["api_key_blob"] is None
        assert body["api_key_iv"] is None


class TestPutAiConfig:
    """PUT /api/v1/ai-config — クライアント暗号化済 blob を保存。"""

    def _put(self, client, **overrides):
        payload = {
            "provider": "openai",
            "api_key_blob": b64encode(b"\xAA" * 48).decode(),
            "api_key_iv": b64encode(b"\xBB" * 12).decode(),
            "model_name": "gpt-4o-mini",
            "custom_prompt": "",
            "compliance_check": False,
        }
        payload.update(overrides)
        return client.put(
            "/api/v1/ai-config",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_unauthenticated(self, client):
        resp = self._put(client)
        assert resp.status_code in (302, 401)

    def test_create_new(self, db, logged_in_client, user, accounts):
        resp = self._put(logged_in_client)
        assert resp.status_code == 200
        cfg = UserAIConfig.query.filter_by(user_id=user.id).first()
        assert cfg is not None
        assert cfg.api_key_blob == b"\xAA" * 48
        assert cfg.api_key_iv == b"\xBB" * 12

    def test_invalid_provider(self, logged_in_client, accounts):
        resp = self._put(logged_in_client, provider="evil-provider")
        assert resp.status_code == 400
        assert "provider" in resp.get_json()["error"]

    def test_invalid_iv_length(self, logged_in_client, accounts):
        resp = self._put(
            logged_in_client,
            api_key_iv=b64encode(b"\x00" * 8).decode(),  # 8B != 12B
        )
        assert resp.status_code == 400
        assert "iv" in resp.get_json()["error"].lower()

    def test_blob_too_large(self, logged_in_client, accounts):
        # 1024B 超で reject
        resp = self._put(
            logged_in_client,
            api_key_blob=b64encode(b"X" * 2000).decode(),
        )
        assert resp.status_code == 400
        assert "too large" in resp.get_json()["error"]

    def test_missing_blob(self, logged_in_client, accounts):
        resp = self._put(logged_in_client, api_key_blob="")
        assert resp.status_code == 400

    def test_invalid_base64(self, logged_in_client, accounts):
        resp = self._put(logged_in_client, api_key_blob="!!!not base64!!!")
        assert resp.status_code == 400
        assert "base64" in resp.get_json()["error"]

    def test_idor_only_own_config(
        self, db, logged_in_client, user, accounts, auditor,
    ):
        # 他ユーザーに既存の config があっても影響なし
        _ai_config(auditor.id, with_e2ee=True)
        # 他人の blob を別値に置く (本人の PUT で他人を上書きしないことを確認)
        other_pre = UserAIConfig.query.filter_by(user_id=auditor.id).first()
        other_pre.api_key_blob = b"\xCC" * 48
        db.session.commit()

        resp = self._put(logged_in_client)
        assert resp.status_code == 200
        # 本人の config が作成され、他人の config は変更なし
        own = UserAIConfig.query.filter_by(user_id=user.id).first()
        other = UserAIConfig.query.filter_by(user_id=auditor.id).first()
        assert own.api_key_blob == b"\xAA" * 48
        assert other.api_key_blob == b"\xCC" * 48


class TestDeleteAiConfig:
    def test_unauthenticated(self, client):
        resp = client.delete("/api/v1/ai-config")
        assert resp.status_code in (302, 401)

    def test_delete_no_config_returns_204(self, logged_in_client, accounts):
        resp = logged_in_client.delete("/api/v1/ai-config")
        assert resp.status_code == 204

    def test_delete_removes_config(
        self, db, logged_in_client, user, accounts,
    ):
        _ai_config(user.id, with_e2ee=True)
        resp = logged_in_client.delete("/api/v1/ai-config")
        assert resp.status_code == 204
        assert UserAIConfig.query.filter_by(user_id=user.id).first() is None


# 旧 TestMigrateKey / TestCliMigrationStatus / TestCliResetMigrateKey は
# Phase E2-b で Fernet 完全廃止 + migrate-key endpoint + CLI 削除に伴い削除。
