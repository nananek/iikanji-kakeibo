"""E2EE 化された /api/v1/ai-config API のテスト (E2 Phase E2-a)。

設計書 §11.5 / §11.7 完了条件のうち、サーバ側 API + migrate-key + CLI を網羅。
クライアント側 (Web ブラウザの AES-GCM 暗号化) は E2-B で別途。
"""

import json
from base64 import b64decode, b64encode
from datetime import datetime, timezone

import pytest

from app.extensions import db
from app.models.ai_config import UserAIConfig


def _ai_config(user_id, *, with_legacy=True, with_e2ee=False, migrated=False):
    """テスト用 UserAIConfig 生成ヘルパー。"""
    cfg = UserAIConfig(
        user_id=user_id,
        provider="openai",
        model_name="gpt-4o-mini",
        custom_prompt="",
        compliance_check=False,
    )
    if with_legacy:
        # 実際の Fernet 暗号化値 (decrypt_api_key で復号可能なもの)
        from app.services.ai_receipt import encrypt_api_key
        cfg.api_key_encrypted = encrypt_api_key("sk-test-legacy-key")
    if with_e2ee:
        cfg.api_key_blob = b"\xAA" * 48  # ダミー暗号文 (48B = 32B 鍵 + 16B タグ)
        cfg.api_key_iv = b"\xBB" * 12
    if migrated:
        cfg.migrated_at = datetime.now(timezone.utc)
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

    def test_returns_metadata_without_legacy_plaintext(
        self, db, logged_in_client, user, accounts,
    ):
        _ai_config(user.id, with_legacy=True)
        resp = logged_in_client.get("/api/v1/ai-config")
        assert resp.status_code == 200
        body = resp.get_json()
        # provider と is_e2ee は返るが平文 api_key は返らない
        assert body["provider"] == "openai"
        assert body["is_e2ee"] is False
        assert body["has_legacy_key"] is True
        assert body["api_key_blob"] is None
        # 旧 Fernet 暗号文も返さない (api_key_encrypted は serialize 対象外)
        assert "api_key_encrypted" not in body

    def test_returns_blob_and_iv_when_e2ee(
        self, db, logged_in_client, user, accounts,
    ):
        _ai_config(user.id, with_legacy=False, with_e2ee=True, migrated=True)
        resp = logged_in_client.get("/api/v1/ai-config")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["is_e2ee"] is True
        # base64 デコードして元のバイト列が返ってきていること
        assert b64decode(body["api_key_blob"]) == b"\xAA" * 48
        assert b64decode(body["api_key_iv"]) == b"\xBB" * 12


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
        assert cfg.api_key_encrypted is None  # 新規作成時に Fernet 形式は使わない

    def test_update_clears_legacy_field(
        self, db, logged_in_client, user, accounts,
    ):
        """既存 legacy Fernet 形式があっても PUT で blob 保存時に NULL クリア。"""
        _ai_config(user.id, with_legacy=True)
        resp = self._put(logged_in_client)
        assert resp.status_code == 200
        cfg = UserAIConfig.query.filter_by(user_id=user.id).first()
        assert cfg.api_key_encrypted is None
        assert cfg.api_key_blob == b"\xAA" * 48

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
        _ai_config(auditor.id, with_legacy=True)
        resp = self._put(logged_in_client)
        assert resp.status_code == 200
        # 本人の config が作成され、他人の config に変更なし
        own = UserAIConfig.query.filter_by(user_id=user.id).first()
        other = UserAIConfig.query.filter_by(user_id=auditor.id).first()
        assert own.api_key_blob == b"\xAA" * 48
        assert other.api_key_blob is None  # 他人は触らない


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


class TestMigrateKey:
    """POST /api/v1/ai-config/migrate-key — per-user 1 回限り。"""

    def test_unauthenticated(self, client):
        resp = client.post("/api/v1/ai-config/migrate-key")
        assert resp.status_code in (302, 401)

    def test_no_config_returns_404(self, logged_in_client, accounts):
        resp = logged_in_client.post("/api/v1/ai-config/migrate-key")
        assert resp.status_code == 404

    def test_returns_plaintext_and_clears_legacy(
        self, db, logged_in_client, user, accounts,
    ):
        cfg = _ai_config(user.id, with_legacy=True)
        assert cfg.api_key_encrypted is not None
        assert cfg.migrated_at is None
        resp = logged_in_client.post("/api/v1/ai-config/migrate-key")
        assert resp.status_code == 200
        body = resp.get_json()
        # 復号された平文が返る
        assert body["api_key"] == "sk-test-legacy-key"
        assert body["provider"] == "openai"
        # 旧カラムが即時 NULL クリア
        db.session.refresh(cfg)
        assert cfg.api_key_encrypted is None
        # migrated_at が set
        assert cfg.migrated_at is not None

    def test_second_call_rejected_410(
        self, db, logged_in_client, user, accounts,
    ):
        """per-user 1 回限り: 2 回目の呼出は 410 Gone。"""
        _ai_config(user.id, with_legacy=True)
        # 1 回目: 成功
        resp1 = logged_in_client.post("/api/v1/ai-config/migrate-key")
        assert resp1.status_code == 200
        # 2 回目: 拒否
        resp2 = logged_in_client.post("/api/v1/ai-config/migrate-key")
        assert resp2.status_code == 410
        assert "already migrated" in resp2.get_json()["error"]

    def test_call_with_no_legacy_marks_as_migrated(
        self, db, logged_in_client, user, accounts,
    ):
        """E2EE 形式で新規登録されたユーザー (api_key_encrypted が None) は
        migrate 対象なし。再呼出防止のため migrated_at をセットして 404 返却。"""
        cfg = _ai_config(user.id, with_legacy=False, with_e2ee=True)
        assert cfg.api_key_encrypted is None
        resp = logged_in_client.post("/api/v1/ai-config/migrate-key")
        assert resp.status_code == 404
        db.session.refresh(cfg)
        assert cfg.migrated_at is not None  # 再呼出ガード成立

    def test_idor_only_own_config(
        self, db, logged_in_client, user, accounts, auditor,
    ):
        """他ユーザーの api_key を取得できないこと。"""
        _ai_config(auditor.id, with_legacy=True)
        # 本人の config なしで呼ぶ
        resp = logged_in_client.post("/api/v1/ai-config/migrate-key")
        # 本人の config が無いので 404 (他人のキーは漏洩しない)
        assert resp.status_code == 404
        # 他人の config は手付かず
        other = UserAIConfig.query.filter_by(user_id=auditor.id).first()
        assert other.api_key_encrypted is not None
        assert other.migrated_at is None


class TestCliMigrationStatus:
    """flask ai-config-migration-status コマンド。"""

    def test_outputs_zero_when_empty(self, app, db, capsys):
        runner = app.test_cli_runner()
        result = runner.invoke(args=["ai-config-migration-status"])
        assert result.exit_code == 0
        assert "total=0" in result.output

    def test_outputs_legacy_and_e2ee_counts(
        self, app, db, user, auditor, accounts,
    ):
        # legacy 1 件 + e2ee migrated 1 件
        _ai_config(user.id, with_legacy=True)
        _ai_config(auditor.id, with_legacy=False, with_e2ee=True, migrated=True)
        runner = app.test_cli_runner()
        result = runner.invoke(args=["ai-config-migration-status"])
        assert result.exit_code == 0
        assert "total=2" in result.output
        assert "legacy_remaining=1" in result.output
        assert "e2ee_migrated=1" in result.output
        assert "migrate_key_called=1" in result.output
