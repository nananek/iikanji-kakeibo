"""CSRF 保護テスト — CSRFトークン無しのPOSTが拒否されることを検証"""

import re

import pytest

from app.extensions import db as _db
from app.models.user import User


def _create_user_and_login(csrf_client, csrf_app):
    """CSRF テスト用ユーザー作成＋セッション設定"""
    with csrf_app.app_context():
        u = User(username="csrfuser", email="csrf@test.com", user_type="personal")
        u.set_password("password123")
        _db.session.add(u)
        _db.session.commit()
        uid = u.id
    with csrf_client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return uid


class TestCsrfRejection:
    """CSRFトークン無しのPOSTが 400 で拒否される"""

    def test_login_without_csrf_rejected(self, csrf_client, csrf_app):
        resp = csrf_client.post("/login", data={
            "username": "test",
            "password": "test",
        })
        assert resp.status_code == 400

    def test_register_without_csrf_rejected(self, csrf_client, csrf_app):
        resp = csrf_client.post("/register", data={
            "username": "newuser",
            "email": "new@test.com",
            "password": "testpass123",
            "password_confirm": "testpass123",
        })
        assert resp.status_code == 400

    def test_cashbook_new_without_csrf_rejected(self, csrf_app, csrf_client):
        _create_user_and_login(csrf_client, csrf_app)
        resp = csrf_client.post("/cashbook/new", data={
            "date": "2026-01-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": 1000,
            "description": "test",
        })
        assert resp.status_code == 400

    def test_journal_delete_without_csrf_rejected(self, csrf_app, csrf_client):
        _create_user_and_login(csrf_client, csrf_app)
        resp = csrf_client.post("/journal/1/delete")
        assert resp.status_code == 400

    def test_settings_passkey_delete_without_csrf_rejected(self, csrf_app, csrf_client):
        _create_user_and_login(csrf_client, csrf_app)
        resp = csrf_client.post("/settings/passkeys/1/delete")
        assert resp.status_code == 400

    def test_settings_api_key_delete_without_csrf_rejected(self, csrf_app, csrf_client):
        _create_user_and_login(csrf_client, csrf_app)
        resp = csrf_client.post("/settings/api-keys/1/delete")
        assert resp.status_code == 400


class TestCsrfExemption:
    """CSRF 免除されている Blueprint の確認"""

    def test_api_exempt_from_csrf(self, csrf_app, csrf_client):
        """API blueprint (/api/v1/*) は CSRF 免除 → 401（認証エラー）が返る"""
        resp = csrf_client.get("/api/v1/journals",
                               headers={"Authorization": "Bearer ik_invalid"})
        # CSRF エラー (400) ではなく認証エラー (401) であること
        assert resp.status_code == 401

    def test_api_post_exempt_from_csrf(self, csrf_app, csrf_client):
        """API POST も CSRF 免除"""
        resp = csrf_client.post("/api/v1/journals",
                                headers={"Authorization": "Bearer ik_invalid"},
                                json={"date": "2026-01-01", "description": "x"})
        assert resp.status_code == 401


class TestCsrfAcceptance:
    """正規CSRFトークン付きリクエストが通ること"""

    def test_valid_csrf_token_accepted(self, csrf_app, csrf_client):
        """CSRFトークン付きログインが CSRF で拒否されない"""
        resp = csrf_client.get("/login")
        html = resp.data.decode()
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
        assert match, "CSRF token not found in login form"
        token = match.group(1)

        resp = csrf_client.post("/login", data={
            "csrf_token": token,
            "username": "nonexistent",
            "password": "whatever",
        })
        # CSRF エラー (400) ではなくログインフォームの再表示 (200)
        assert resp.status_code == 200
