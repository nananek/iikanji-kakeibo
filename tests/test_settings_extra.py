"""設定ビュー (settings.py) の追加テスト

既存の test_settings.py は index/display 周辺を扱う。
こちらは passkeys / ai / api-keys / fiscal / audit / tax-form 等を網羅。
"""

from datetime import datetime, timezone

import pytest

from app.models.ai_config import UserAIConfig
from app.models.api_key import APIKey
from app.models.audit import AuditGrant, AuditGrantAccount
from app.models.fiscal import FiscalClose
from app.models.webauthn import WebAuthnCredential


class TestPasskeys:
    def test_unauthenticated(self, client):
        resp = client.get("/settings/passkeys")
        assert resp.status_code in (302, 401)

    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/passkeys")
        assert resp.status_code == 200

    def test_delete(self, db, logged_in_client, user, accounts):
        cred = WebAuthnCredential(
            user_id=user.id,
            credential_id=b"\x01\x02\x03",
            credential_public_key=b"pub",
            current_sign_count=0,
            name="Mine",
        )
        db.session.add(cred)
        db.session.commit()
        cid = cred.id
        resp = logged_in_client.post(f"/settings/passkeys/{cid}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(WebAuthnCredential, cid) is None

    def test_delete_hx(self, db, logged_in_client, user, accounts):
        cred = WebAuthnCredential(
            user_id=user.id, credential_id=b"\x10",
            credential_public_key=b"pub", current_sign_count=0,
        )
        db.session.add(cred)
        db.session.commit()
        resp = logged_in_client.post(
            f"/settings/passkeys/{cred.id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers

    def test_idor(self, db, logged_in_client, accounts, second_user):
        cred = WebAuthnCredential(
            user_id=second_user.id, credential_id=b"\x99",
            credential_public_key=b"pub", current_sign_count=0,
        )
        db.session.add(cred)
        db.session.commit()
        resp = logged_in_client.post(f"/settings/passkeys/{cred.id}/delete")
        assert resp.status_code == 404


class TestAiConfig:
    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/ai")
        assert resp.status_code == 200

    def test_save_new_openai(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/settings/ai/save", data={
            "provider": "openai",
            "api_key": "sk-test",
            "model_name": "gpt-4",
            "custom_prompt": "",
            "base_url": "",
        })
        assert resp.status_code in (302, 303)
        cfg = UserAIConfig.query.filter_by(user_id=user.id).first()
        assert cfg is not None
        assert cfg.provider == "openai"

    def test_save_update_existing_keeps_key(self, db, logged_in_client, user, accounts):
        from app.services.ai_receipt import encrypt_api_key
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_encrypted=encrypt_api_key("orig"), model_name="g3",
        )
        db.session.add(cfg)
        db.session.commit()
        original_key = cfg.api_key_encrypted
        resp = logged_in_client.post("/settings/ai/save", data={
            "provider": "anthropic",
            "api_key": "",  # 空のときは既存キー保持
            "model_name": "claude",
            "custom_prompt": "",
            "base_url": "",
        })
        assert resp.status_code in (302, 303)
        db.session.refresh(cfg)
        assert cfg.provider == "anthropic"
        assert cfg.api_key_encrypted == original_key

    def test_save_invalid_provider(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/ai/save", data={
            "provider": "INVALID", "api_key": "k",
        })
        assert resp.status_code in (302, 303)

    def test_save_llama_cpp_no_key(self, db, logged_in_client, user, accounts):
        """llama.cpp は API キー不要で保存できる (サーバー側 URL 提供前提)"""
        resp = logged_in_client.post("/settings/ai/save", data={
            "provider": "llama_cpp",
            "api_key": "",
            "model_name": "default",
        })
        assert resp.status_code in (302, 303)
        cfg = UserAIConfig.query.filter_by(user_id=user.id).first()
        assert cfg is not None
        assert cfg.provider == "llama_cpp"

    def test_save_no_key_for_openai_blocked(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/settings/ai/save", data={
            "provider": "openai", "api_key": "",
        })
        assert resp.status_code in (302, 303)
        assert UserAIConfig.query.filter_by(user_id=user.id).first() is None

    def test_delete(self, db, logged_in_client, user, accounts):
        from app.services.ai_receipt import encrypt_api_key
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_encrypted=encrypt_api_key("k"), model_name="x",
        )
        db.session.add(cfg)
        db.session.commit()
        resp = logged_in_client.post("/settings/ai/delete")
        assert resp.status_code in (302, 303)
        assert UserAIConfig.query.filter_by(user_id=user.id).first() is None


class TestApiKeys:
    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/api-keys")
        assert resp.status_code == 200

    def test_create(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/settings/api-keys/create", data={
            "name": "MyKey",
            "scopes": ["journals:create", "journals:read"],
        })
        assert resp.status_code == 200
        assert APIKey.query.filter_by(user_id=user.id, name="MyKey").count() == 1

    def test_create_no_scopes_defaults(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/settings/api-keys/create", data={
            "name": "DefScope",
        })
        assert resp.status_code == 200
        key = APIKey.query.filter_by(user_id=user.id, name="DefScope").first()
        assert key is not None
        assert "journals:create" in key.scopes

    def test_create_no_name(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/api-keys/create", data={"name": ""})
        assert resp.status_code in (302, 303)

    def test_create_resolves_dependencies(self, db, logged_in_client, user, accounts):
        # journals:delete を選ぶと journals:read も自動付与
        resp = logged_in_client.post("/settings/api-keys/create", data={
            "name": "DelKey",
            "scopes": ["journals:delete"],
        })
        assert resp.status_code == 200
        key = APIKey.query.filter_by(user_id=user.id, name="DelKey").first()
        assert "journals:read" in key.scopes
        assert "journals:delete" in key.scopes

    def test_delete(self, db, logged_in_client, user, accounts):
        raw, h, prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="K", key_hash=h,
            key_prefix=prefix, scopes="journals:create",
        )
        db.session.add(key)
        db.session.commit()
        kid = key.id
        resp = logged_in_client.post(f"/settings/api-keys/{kid}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(APIKey, kid) is None

    def test_delete_hx(self, db, logged_in_client, user, accounts):
        raw, h, prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="K", key_hash=h,
            key_prefix=prefix, scopes="journals:create",
        )
        db.session.add(key)
        db.session.commit()
        resp = logged_in_client.post(
            f"/settings/api-keys/{key.id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


class TestFiscal:
    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/fiscal")
        assert resp.status_code == 200

    def test_get_with_year(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/fiscal?year=2025")
        assert resp.status_code == 200

    def test_open_year(self, db, logged_in_client, user, accounts):
        # 2023 は前々年なので未開設、open-year で FiscalClose レコード作成される
        resp = logged_in_client.post("/settings/fiscal/open-year", data={
            "year": "2023",
        })
        assert resp.status_code in (302, 303)
        assert FiscalClose.query.filter_by(
            user_id=user.id, year=2023
        ).first() is not None

    def test_open_year_already_open(self, db, logged_in_client, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2023, closed_period=-1))
        db.session.commit()
        resp = logged_in_client.post("/settings/fiscal/open-year", data={
            "year": "2023",
        })
        assert resp.status_code in (302, 303)
        # 重複なし
        assert FiscalClose.query.filter_by(
            user_id=user.id, year=2023
        ).count() == 1

    def test_close_period(self, db, logged_in_client, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=-1))
        db.session.commit()
        resp = logged_in_client.post("/settings/fiscal/close", data={
            "year": "2026", "period": "0",
        })
        assert resp.status_code in (302, 303)

    def test_close_period_hx(self, db, logged_in_client, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=-1))
        db.session.commit()
        resp = logged_in_client.post(
            "/settings/fiscal/close",
            data={"year": "2026", "period": "0"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code in (200, 422)
        assert "HX-Trigger" in resp.headers

    def test_close_period_ajax(self, db, logged_in_client, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=-1))
        db.session.commit()
        resp = logged_in_client.post(
            "/settings/fiscal/close",
            data={"year": "2026", "period": "0"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code in (200, 400)

    def test_reopen_period(self, db, logged_in_client, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post("/settings/fiscal/reopen", data={
            "year": "2026", "period": "2",
        })
        assert resp.status_code in (302, 303)


class TestAuditSettings:
    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/audit")
        assert resp.status_code == 200

    def test_auditor_blocked(self, client, auditor):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/settings/audit")
        assert resp.status_code in (302, 303)

    def test_add_grant_lv3(self, db, logged_in_client, user, accounts, auditor):
        resp = logged_in_client.post("/settings/audit/add", data={
            "username": auditor.username,
            "permission_level": "3",
        })
        assert resp.status_code in (302, 303)
        assert AuditGrant.query.filter_by(
            owner_user_id=user.id, auditor_user_id=auditor.id
        ).count() == 1

    def test_add_grant_lv2_seeds_accounts(self, db, logged_in_client, user, accounts, auditor):
        # tax_category 付き科目を作る
        accounts["5010"].tax_category = "office_supplies"
        db.session.commit()
        resp = logged_in_client.post("/settings/audit/add", data={
            "username": auditor.username,
            "permission_level": "2",
        })
        assert resp.status_code in (302, 303)
        g = AuditGrant.query.filter_by(
            owner_user_id=user.id, auditor_user_id=auditor.id
        ).first()
        codes = {ga.account_code for ga in g.grant_accounts}
        # 5010 (tax_category 付き) と 3030 (proprietor) が自動公開
        assert "5010" in codes
        assert "3030" in codes

    def test_add_grant_invalid_level(self, db, logged_in_client, accounts, auditor):
        resp = logged_in_client.post("/settings/audit/add", data={
            "username": auditor.username,
            "permission_level": "99",
        })
        assert resp.status_code in (302, 303)
        assert AuditGrant.query.count() == 0

    def test_add_grant_unknown_user(self, db, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/audit/add", data={
            "username": "ghost",
            "permission_level": "3",
        })
        assert resp.status_code in (302, 303)
        assert AuditGrant.query.count() == 0

    def test_add_duplicate_grant(self, db, logged_in_client, user, accounts, auditor):
        existing = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=1,
        )
        db.session.add(existing)
        db.session.commit()
        resp = logged_in_client.post("/settings/audit/add", data={
            "username": auditor.username,
            "permission_level": "3",
        })
        assert resp.status_code in (302, 303)
        # 重複追加されない
        assert AuditGrant.query.filter_by(
            owner_user_id=user.id, auditor_user_id=auditor.id
        ).count() == 1

    def test_delete_grant(self, db, logged_in_client, user, accounts, auditor):
        g = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=3,
        )
        db.session.add(g)
        db.session.commit()
        resp = logged_in_client.post(f"/settings/audit/{g.id}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(AuditGrant, g.id) is None

    def test_idor_other_user_grant_404(self, db, logged_in_client, accounts,
                                        second_user, auditor):
        g = AuditGrant(
            owner_user_id=second_user.id, auditor_user_id=auditor.id,
            permission_level=3,
        )
        db.session.add(g)
        db.session.commit()
        resp = logged_in_client.post(f"/settings/audit/{g.id}/delete")
        assert resp.status_code == 404

    def test_submit_lv2(self, db, logged_in_client, user, accounts, auditor):
        g = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="draft",
        )
        db.session.add(g)
        db.session.commit()
        resp = logged_in_client.post(f"/settings/audit/{g.id}/submit")
        assert resp.status_code in (302, 303)
        db.session.refresh(g)
        assert g.status == "submitted"

    def test_unsubmit_lv2(self, db, logged_in_client, user, accounts, auditor):
        g = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="submitted",
            submitted_at=datetime.now(timezone.utc),
        )
        db.session.add(g)
        db.session.commit()
        resp = logged_in_client.post(f"/settings/audit/{g.id}/unsubmit")
        assert resp.status_code in (302, 303)
        db.session.refresh(g)
        assert g.status == "draft"

    def test_audit_accounts_get(self, db, logged_in_client, user, accounts, auditor):
        g = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2,
        )
        db.session.add(g)
        db.session.commit()
        resp = logged_in_client.get(f"/settings/audit/{g.id}/accounts")
        assert resp.status_code == 200

    def test_audit_accounts_save(self, db, logged_in_client, user, accounts, auditor):
        g = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2,
        )
        db.session.add(g)
        db.session.commit()
        resp = logged_in_client.post(
            f"/settings/audit/{g.id}/accounts",
            data={"account_codes": ["5010", "1010"]},
        )
        assert resp.status_code in (302, 303)
        codes = {ga.account_code for ga in g.grant_accounts}
        assert "5010" in codes
        assert "1010" in codes
        # 事業主 (3030) は自動追加
        assert "3030" in codes

    def test_audit_accounts_save_blocked_when_submitted(self, db, logged_in_client,
                                                         user, accounts, auditor):
        g = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="submitted",
        )
        db.session.add(g)
        db.session.commit()
        resp = logged_in_client.post(
            f"/settings/audit/{g.id}/accounts",
            data={"account_codes": ["5010"]},
        )
        assert resp.status_code in (302, 303)
        # 保存されない
        assert AuditGrantAccount.query.filter_by(audit_grant_id=g.id).count() == 0


class TestTaxForm:
    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/tax-form")
        assert resp.status_code == 200

    def test_get_real_estate(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/tax-form?form_type=real_estate")
        assert resp.status_code == 200

    def test_get_invalid_form_type(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/tax-form?form_type=BAD")
        assert resp.status_code == 200


class TestAutoImport:
    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/auto-import")
        assert resp.status_code == 200


class TestOAuthTokens:
    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/oauth-tokens")
        assert resp.status_code == 200
