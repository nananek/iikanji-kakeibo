"""OAuth Device Authorization Grant (RFC 8628) のテスト"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.oauth import (
    OAuthDevice, OAuthToken,
    DEVICE_CODE_EXPIRES_IN, DEVICE_CODE_POLL_INTERVAL,
)


DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _post_token(client, device_code):
    return client.post(
        "/oauth/token",
        data=json.dumps({
            "grant_type": DEVICE_GRANT,
            "device_code": device_code,
        }),
        content_type="application/json",
    )


class TestDeviceAuthorization:
    """POST /oauth/device — device_code/user_code 発行"""

    def test_issues_device_code(self, client):
        resp = client.post("/oauth/device", json={"client_name": "TUI v0.1"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert "device_code" in body
        assert "user_code" in body
        assert body["expires_in"] == DEVICE_CODE_EXPIRES_IN
        assert body["interval"] == DEVICE_CODE_POLL_INTERVAL
        assert body["verification_uri"].endswith("/oauth/device")
        assert body["user_code"] in body["verification_uri_complete"]

    def test_user_code_format_xxxx_xxxx(self, client):
        resp = client.post("/oauth/device")
        body = resp.get_json()
        code = body["user_code"]
        assert len(code) == 9
        assert code[4] == "-"
        # I, O, 0, 1 を含まない
        assert not any(c in code for c in "IO01")

    def test_device_code_persists_only_hash(self, client, db):
        resp = client.post("/oauth/device")
        raw = resp.get_json()["device_code"]
        # raw は DB に保存されていない
        all_devices = OAuthDevice.query.all()
        assert len(all_devices) == 1
        assert all_devices[0].device_code_hash != raw
        assert all_devices[0].device_code_hash == OAuthDevice.hash_device_code(raw)

    def test_unique_user_codes(self, client):
        codes = set()
        for _ in range(5):
            resp = client.post("/oauth/device")
            codes.add(resp.get_json()["user_code"])
        assert len(codes) == 5

    def test_no_authentication_required(self, client):
        resp = client.post("/oauth/device", json={})
        assert resp.status_code == 200

    def test_client_name_truncated(self, client, db):
        long_name = "X" * 200
        resp = client.post("/oauth/device", json={"client_name": long_name})
        assert resp.status_code == 200
        device = OAuthDevice.query.first()
        assert device.client_name is not None
        assert len(device.client_name) <= 100


class TestTokenPolling:
    """POST /oauth/token — クライアントのポーリング"""

    def test_pending_returns_authorization_pending(self, client):
        resp = client.post("/oauth/device")
        device_code = resp.get_json()["device_code"]
        token_resp = _post_token(client, device_code)
        assert token_resp.status_code == 400
        assert token_resp.get_json()["error"] == "authorization_pending"

    def test_unknown_device_code_returns_invalid_grant(self, client):
        resp = _post_token(client, "nonexistent")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_grant"

    def test_unsupported_grant_type(self, client):
        resp = client.post(
            "/oauth/token",
            data=json.dumps({
                "grant_type": "password",
                "device_code": "x",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "unsupported_grant_type"

    def test_missing_device_code(self, client):
        resp = client.post(
            "/oauth/token",
            data=json.dumps({"grant_type": DEVICE_GRANT}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_request"

    def test_slow_down_when_polling_too_fast(self, client, db):
        resp = client.post("/oauth/device")
        device_code = resp.get_json()["device_code"]
        # 最初のポール
        first = _post_token(client, device_code)
        assert first.get_json()["error"] == "authorization_pending"
        # 即座に2回目 → slow_down
        second = _post_token(client, device_code)
        assert second.status_code == 400
        assert second.get_json()["error"] == "slow_down"

    def test_expired_device_code(self, client, db):
        resp = client.post("/oauth/device")
        device_code = resp.get_json()["device_code"]
        # expires_at を過去にする
        device = OAuthDevice.query.first()
        device.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        db.session.commit()
        token_resp = _post_token(client, device_code)
        assert token_resp.status_code == 400
        assert token_resp.get_json()["error"] == "expired_token"

    def test_denied_device_code(self, client, db):
        resp = client.post("/oauth/device")
        device_code = resp.get_json()["device_code"]
        device = OAuthDevice.query.first()
        device.status = "denied"
        db.session.commit()
        token_resp = _post_token(client, device_code)
        assert token_resp.status_code == 400
        assert token_resp.get_json()["error"] == "access_denied"

    def test_approved_returns_access_token(self, client, db, user):
        resp = client.post("/oauth/device", json={"client_name": "TUI"})
        device_code = resp.get_json()["device_code"]
        device = OAuthDevice.query.first()
        device.status = "approved"
        device.user_id = user.id
        db.session.commit()
        token_resp = _post_token(client, device_code)
        assert token_resp.status_code == 200
        body = token_resp.get_json()
        assert body["token_type"] == "Bearer"
        assert body["access_token"].startswith("ikt_")
        assert body["expires_in"] == 31536000

        token = OAuthToken.query.filter_by(user_id=user.id).first()
        assert token is not None
        assert token.is_active is True
        assert token.token_hash == OAuthToken.hash_token(body["access_token"])
        # device は consumed 状態
        db.session.refresh(device)
        assert device.status == "consumed"

    def test_consumed_device_code_returns_invalid_grant(self, client, db, user):
        resp = client.post("/oauth/device")
        device_code = resp.get_json()["device_code"]
        device = OAuthDevice.query.first()
        device.status = "approved"
        device.user_id = user.id
        db.session.commit()
        # 1回目で consume
        first = _post_token(client, device_code)
        assert first.status_code == 200
        # 2回目は invalid_grant
        # 最初に slow_down 回避のため last_polled_at をリセット
        device = OAuthDevice.query.first()
        device.last_polled_at = None
        db.session.commit()
        second = _post_token(client, device_code)
        assert second.status_code == 400
        assert second.get_json()["error"] == "invalid_grant"

    def test_form_encoded_request_accepted(self, client):
        # OAuth 2.0 標準は application/x-www-form-urlencoded
        resp = client.post("/oauth/device")
        device_code = resp.get_json()["device_code"]
        token_resp = client.post("/oauth/token", data={
            "grant_type": DEVICE_GRANT,
            "device_code": device_code,
        })
        assert token_resp.status_code == 400
        assert token_resp.get_json()["error"] == "authorization_pending"


class TestDeviceVerificationView:
    """GET /oauth/device — ユーザーがブラウザで承認する画面"""

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/oauth/device")
        assert resp.status_code in (302, 401)

    def test_logged_in_shows_form(self, logged_in_client):
        resp = logged_in_client.get("/oauth/device")
        assert resp.status_code == 200
        assert "デバイスコード" in resp.get_data(as_text=True)

    def test_logged_in_with_valid_code_shows_approve(self, logged_in_client, db):
        # device 作成
        resp = logged_in_client.post("/oauth/device", json={"client_name": "TUI"})
        user_code = resp.get_json()["user_code"]
        page = logged_in_client.get(f"/oauth/device?code={user_code}")
        body = page.get_data(as_text=True)
        assert page.status_code == 200
        assert "承認" in body
        assert "拒否" in body
        assert "TUI" in body

    def test_unknown_user_code_shows_form_only(self, logged_in_client):
        page = logged_in_client.get("/oauth/device?code=ZZZZ-ZZZZ")
        assert page.status_code == 200
        # unknown は device=None として扱われ、入力フォーム再表示
        assert "デバイスコード" in page.get_data(as_text=True)

    def test_expired_code_shows_expired_message(self, logged_in_client, db):
        resp = logged_in_client.post("/oauth/device")
        user_code = resp.get_json()["user_code"]
        device = OAuthDevice.query.first()
        device.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        db.session.commit()
        page = logged_in_client.get(f"/oauth/device?code={user_code}")
        assert "期限切れ" in page.get_data(as_text=True)


class TestDeviceAuthorize:
    """POST /oauth/device/authorize — 承認/拒否処理"""

    def test_unauthenticated_redirects(self, client):
        resp = client.post("/oauth/device/authorize", data={
            "user_code": "AAAA-BBBB", "decision": "approve",
        })
        assert resp.status_code in (302, 401)

    def test_approve_sets_status_and_user_id(self, logged_in_client, db, user):
        resp = logged_in_client.post("/oauth/device", json={"client_name": "TUI"})
        user_code = resp.get_json()["user_code"]
        approve = logged_in_client.post("/oauth/device/authorize", data={
            "user_code": user_code, "decision": "approve",
        })
        assert approve.status_code in (302, 303)
        device = OAuthDevice.query.first()
        assert device.status == "approved"
        assert device.user_id == user.id

    def test_deny_sets_status_denied(self, logged_in_client, db, user):
        resp = logged_in_client.post("/oauth/device")
        user_code = resp.get_json()["user_code"]
        deny = logged_in_client.post("/oauth/device/authorize", data={
            "user_code": user_code, "decision": "deny",
        })
        assert deny.status_code in (302, 303)
        device = OAuthDevice.query.first()
        assert device.status == "denied"
        assert device.user_id is None

    def test_unknown_code_redirects_with_flash(self, logged_in_client):
        resp = logged_in_client.post("/oauth/device/authorize", data={
            "user_code": "ZZZZ-ZZZZ", "decision": "approve",
        })
        assert resp.status_code in (302, 303)

    def test_already_approved_cannot_be_changed(self, logged_in_client, db, user):
        resp = logged_in_client.post("/oauth/device")
        user_code = resp.get_json()["user_code"]
        # 一度承認
        logged_in_client.post("/oauth/device/authorize", data={
            "user_code": user_code, "decision": "approve",
        })
        # 拒否しようとしても上書きできない
        logged_in_client.post("/oauth/device/authorize", data={
            "user_code": user_code, "decision": "deny",
        })
        device = OAuthDevice.query.first()
        assert device.status == "approved"

    def test_user_code_normalization(self, logged_in_client, db, user):
        resp = logged_in_client.post("/oauth/device")
        user_code = resp.get_json()["user_code"]
        without_dash = user_code.replace("-", "").lower()
        approve = logged_in_client.post("/oauth/device/authorize", data={
            "user_code": without_dash, "decision": "approve",
        })
        assert approve.status_code in (302, 303)
        device = OAuthDevice.query.first()
        assert device.status == "approved"


class TestEndToEndFlow:
    """完全なフロー: device 発行 → ユーザー承認 → token 取得 → API 呼出"""

    def test_full_happy_path(self, client, logged_in_client, db, user, accounts):
        # 1. クライアントが device_code 発行
        resp = client.post("/oauth/device", json={"client_name": "TUI v0.1"})
        body = resp.get_json()
        device_code = body["device_code"]
        user_code = body["user_code"]

        # 2. ポーリングは authorization_pending
        poll = _post_token(client, device_code)
        assert poll.get_json()["error"] == "authorization_pending"

        # 3. ユーザーがブラウザで承認
        logged_in_client.post("/oauth/device/authorize", data={
            "user_code": user_code, "decision": "approve",
        })

        # 4. クライアントが再度ポーリング → アクセストークン取得
        # last_polled_at をリセット (slow_down回避)
        device = OAuthDevice.query.first()
        device.last_polled_at = None
        db.session.commit()
        token_resp = _post_token(client, device_code)
        assert token_resp.status_code == 200
        access_token = token_resp.get_json()["access_token"]

        # 5. アクセストークンで API を呼び出し
        list_resp = client.get(
            "/api/v1/journals",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert list_resp.status_code == 200

        # 6. last_used_at が更新されている
        token = OAuthToken.query.filter_by(user_id=user.id).first()
        assert token.last_used_at is not None


class TestBearerAuthIntegration:
    """既存 Bearer 認証への OAuthToken 統合"""

    def _make_token(self, db, user, is_active=True):
        raw, h, prefix = OAuthToken.generate()
        token = OAuthToken(
            user_id=user.id,
            name="test",
            token_hash=h,
            token_prefix=prefix,
            is_active=is_active,
        )
        db.session.add(token)
        db.session.commit()
        return raw, token

    def test_oauth_token_grants_journals_read(self, client, db, user, accounts):
        raw, _ = self._make_token(db, user)
        resp = client.get(
            "/api/v1/journals",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200

    def test_oauth_token_grants_journals_create(self, client, db, user, accounts):
        raw, _ = self._make_token(db, user)
        resp = client.post(
            "/api/v1/journals",
            headers={"Authorization": f"Bearer {raw}"},
            json={
                "date": "2026-01-15",
                "description": "OAuth経由テスト",
                "lines": [
                    {"account_code": "5010", "debit": 1000, "credit": 0},
                    {"account_code": "1010", "debit": 0, "credit": 1000},
                ],
            },
        )
        assert resp.status_code == 201

    def test_revoked_oauth_token_rejected(self, client, db, user):
        raw, token = self._make_token(db, user)
        token.is_active = False
        db.session.commit()
        resp = client.get(
            "/api/v1/journals",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 401

    def test_invalid_oauth_token_rejected(self, client):
        resp = client.get(
            "/api/v1/journals",
            headers={"Authorization": "Bearer ikt_deadbeef"},
        )
        assert resp.status_code == 401

    def test_existing_apikey_still_works(self, client, auth_header, accounts):
        resp = client.get("/api/v1/journals", headers=auth_header)
        assert resp.status_code == 200

    def test_oauth_token_user_isolation(self, client, db, user, second_user, second_user_accounts, accounts):
        """OAuth トークンは発行ユーザーのデータのみアクセス可能"""
        raw, _ = self._make_token(db, second_user)
        # second_user のトークンで /journals を呼ぶ — second_user のデータだけ見える
        resp = client.get(
            "/api/v1/journals",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        # データは空（second_user は仕訳を作っていない）
        assert body["total"] == 0


class TestReadOnlyOAuthToken:
    """read_only な OAuth トークンの権限制御"""

    def _make_ro_token(self, db, user):
        raw, h, prefix = OAuthToken.generate()
        token = OAuthToken(
            user_id=user.id,
            name="ro-test",
            token_hash=h,
            token_prefix=prefix,
            read_only=True,
        )
        db.session.add(token)
        db.session.commit()
        return raw

    def test_readonly_can_read_journals(self, client, db, user, accounts):
        raw = self._make_ro_token(db, user)
        resp = client.get(
            "/api/v1/journals",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200

    def test_readonly_blocks_create_journal(self, client, db, user, accounts):
        raw = self._make_ro_token(db, user)
        resp = client.post(
            "/api/v1/journals",
            headers={"Authorization": f"Bearer {raw}"},
            json={
                "date": "2026-01-15",
                "description": "ro reject test",
                "lines": [
                    {"account_code": "5010", "debit": 100},
                    {"account_code": "1010", "credit": 100},
                ],
            },
        )
        assert resp.status_code == 403
        assert "読み取り専用" in resp.get_json()["error"]

    def test_readonly_blocks_delete_journal(self, client, db, user, accounts):
        from app.services.accounting import create_journal_entry
        from datetime import date as date_type
        entry = create_journal_entry(
            user_id=user.id,
            date=date_type(2026, 1, 15),
            description="del test",
            lines_data=[
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0, "description": ""},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100, "description": ""},
            ],
            source="api",
        )
        db.session.commit()
        raw = self._make_ro_token(db, user)
        resp = client.delete(
            f"/api/v1/journals/{entry.id}",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403


class TestDeviceAuthorizeReadOnly:
    """デバイス承認時の読み取り専用フラグ伝播"""

    def test_approve_readonly_sets_flag_on_device(self, logged_in_client, db, user):
        resp = logged_in_client.post("/oauth/device", json={"client_name": "TUI"})
        user_code = resp.get_json()["user_code"]
        logged_in_client.post("/oauth/device/authorize", data={
            "user_code": user_code, "decision": "approve_readonly",
        })
        device = OAuthDevice.query.first()
        assert device.status == "approved"
        assert device.read_only is True

    def test_approve_default_is_not_readonly(self, logged_in_client, db, user):
        resp = logged_in_client.post("/oauth/device", json={"client_name": "TUI"})
        user_code = resp.get_json()["user_code"]
        logged_in_client.post("/oauth/device/authorize", data={
            "user_code": user_code, "decision": "approve",
        })
        device = OAuthDevice.query.first()
        assert device.read_only is False

    def test_token_inherits_read_only_from_device(self, client, logged_in_client, db, user):
        # 1. デバイス発行
        resp = client.post("/oauth/device")
        device_code = resp.get_json()["device_code"]
        user_code = resp.get_json()["user_code"]
        # 2. ユーザー承認 (読み取り専用)
        logged_in_client.post("/oauth/device/authorize", data={
            "user_code": user_code, "decision": "approve_readonly",
        })
        # 3. トークン取得
        from app.views.oauth import DEVICE_GRANT_TYPE
        # ポーリング間隔を回避するため device の last_polled_at を一旦リセット
        device = OAuthDevice.query.first()
        device.last_polled_at = None
        db.session.commit()
        token_resp = client.post("/oauth/token", json={
            "grant_type": DEVICE_GRANT_TYPE, "device_code": device_code,
        })
        assert token_resp.status_code == 200
        # 4. 発行された OAuthToken は read_only=True を継承
        token = OAuthToken.query.first()
        assert token.read_only is True

    def test_settings_page_shows_readonly_badge(self, logged_in_client, db, user):
        token = OAuthToken(
            user_id=user.id, name="ro-test",
            token_hash="x" * 64, token_prefix="ikt_xx...",
            read_only=True,
        )
        db.session.add(token)
        db.session.commit()
        resp = logged_in_client.get("/settings/oauth-tokens")
        assert resp.status_code == 200
        assert "読取専用" in resp.data.decode()


class TestSettingsOAuthTokens:
    """設定画面の OAuth トークン一覧/取消"""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/settings/oauth-tokens")
        assert resp.status_code in (302, 401)

    def test_list_shows_tokens(self, logged_in_client, db, user):
        raw, h, prefix = OAuthToken.generate()
        token = OAuthToken(
            user_id=user.id, name="TUI",
            token_hash=h, token_prefix=prefix,
        )
        db.session.add(token)
        db.session.commit()
        resp = logged_in_client.get("/settings/oauth-tokens")
        assert resp.status_code == 200
        assert "TUI" in resp.get_data(as_text=True)

    def test_revoke_token(self, logged_in_client, db, user):
        raw, h, prefix = OAuthToken.generate()
        token = OAuthToken(
            user_id=user.id, name="TUI",
            token_hash=h, token_prefix=prefix,
        )
        db.session.add(token)
        db.session.commit()
        token_id = token.id

        resp = logged_in_client.post(f"/settings/oauth-tokens/{token_id}/revoke")
        assert resp.status_code in (302, 303)

        token = db.session.get(OAuthToken, token_id)
        assert token.is_active is False
        assert token.revoked_at is not None

    def test_idor_cannot_revoke_other_users_token(
        self, logged_in_client, db, user, second_user
    ):
        raw, h, prefix = OAuthToken.generate()
        token = OAuthToken(
            user_id=second_user.id, name="他人",
            token_hash=h, token_prefix=prefix,
        )
        db.session.add(token)
        db.session.commit()

        resp = logged_in_client.post(f"/settings/oauth-tokens/{token.id}/revoke")
        assert resp.status_code == 404
        # token は引き続き有効
        db.session.refresh(token)
        assert token.is_active is True
