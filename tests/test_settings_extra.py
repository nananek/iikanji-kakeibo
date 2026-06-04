"""設定ビュー (settings.py) の追加テスト

既存の test_settings.py は index/display 周辺を扱う。
こちらは passkeys / ai / api-keys / fiscal / audit / tax-form 等を網羅。
"""

import pytest

from app.models.ai_config import UserAIConfig
from app.models.api_key import APIKey
from app.models.audit import AuditGrant
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

    # 旧 form POST /settings/ai/save テスト群は E2EE 化に伴いエンドポイント
    # 廃止のため削除。PUT /api/v1/ai-config テストは test_ai_config_api.py で代替。

    def test_delete(self, db, logged_in_client, user, accounts):
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
            model_name="x",
        )
        db.session.add(cfg)
        db.session.commit()
        resp = logged_in_client.post("/settings/ai/delete")
        assert resp.status_code in (302, 303)
        assert UserAIConfig.query.filter_by(user_id=user.id).first() is None

    def test_page_uses_e2ee_alpine_component(
        self, db, logged_in_client, user, accounts,
    ):
        """E2-B: 設定ページが Alpine.js E2EE フォーム (aiConfigForm) を使う。"""
        resp = logged_in_client.get("/settings/ai")
        assert resp.status_code == 200
        body = resp.data.decode()
        # Alpine コンポーネント呼び出し
        assert "aiConfigForm(" in body
        # 暗号鍵管理への誘導リンク
        assert "/settings/encryption-keys" in body
        # E2EE 説明文
        assert "AES-256-GCM" in body or "E2EE" in body
        # ai_config_form.js が読み込まれる
        assert "ai_config_form.js" in body

    def test_page_renders_existing_config_via_alpine(
        self, db, logged_in_client, user, accounts,
    ):
        """既存設定が Alpine 初期値として渡される (provider 等)。

        x-data の JSON 部分に "provider": "anthropic" 等が含まれることを直接
        確認する (`<option value="anthropic">` 等の provider_labels 出力
        と区別するため)。
        """
        cfg = UserAIConfig(
            user_id=user.id, provider="anthropic",
            api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
            model_name="claude-3-5-sonnet", custom_prompt="my prompt",
        )
        db.session.add(cfg)
        db.session.commit()
        resp = logged_in_client.get("/settings/ai")
        body = resp.data.decode()
        # JSON 化された initial 値が x-data 属性に埋め込まれる
        # (HTML escape された &quot; 形式で含まれる)
        assert (
            '&#34;provider&#34;: &#34;anthropic&#34;' in body
            or '"provider": "anthropic"' in body
        )
        assert (
            '&#34;model_name&#34;: &#34;claude-3-5-sonnet&#34;' in body
            or '"model_name": "claude-3-5-sonnet"' in body
        )


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

    def test_close_period_15_rejected(self, db, logged_in_client, user, accounts):
        """#338 item1: 決算月3 (period15) の確定は htmx 経路では受け付けない
        (クライアントが closing を暗号化生成して close-closing エンドポイントへ送る)。
        period14 まで確定済みでも fiscal_close は 422/400 で弾き、FiscalClose は不変。"""
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=14))
        db.session.commit()
        resp = logged_in_client.post(
            "/settings/fiscal/close",
            data={"year": "2026", "period": "15"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422
        assert "HX-Trigger" in resp.headers
        # FiscalClose は 14 のまま (closing も生成されない)
        fc = FiscalClose.query.filter_by(user_id=user.id, year=2026).first()
        assert fc.closed_period == 14
        from app.models.journal import JournalEntry
        assert JournalEntry.query.filter_by(user_id=user.id, is_closing=True).count() == 0


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


class TestAutoImportRemoved:
    """/settings/auto-import は廃止 (404)。"""

    def test_endpoint_removed(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/auto-import")
        assert resp.status_code == 404


class TestOAuthTokens:
    def test_get(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/oauth-tokens")
        assert resp.status_code == 200
