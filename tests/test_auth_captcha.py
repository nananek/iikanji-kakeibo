"""auth.py の CAPTCHA フェイルパステスト

CAPTCHA を有効化して、トークン未送信・検証失敗のケースを検証する。
"""

from unittest.mock import patch

import pytest

from app.models.user import User


@pytest.fixture
def captcha_enabled(app):
    """CAPTCHA を有効にした状態"""
    app.config["CAPTCHA_PROVIDER"] = "hcaptcha"
    app.config["CAPTCHA_SECRET_KEY"] = "secret"
    app.config["CAPTCHA_SITE_KEY"] = "site"
    yield
    app.config["CAPTCHA_PROVIDER"] = None
    app.config["CAPTCHA_SECRET_KEY"] = None
    app.config["CAPTCHA_SITE_KEY"] = None


class TestLoginCaptcha:
    def test_login_no_captcha_token(self, client, user, captcha_enabled):
        # CAPTCHA 有効だがトークンなし
        resp = client.post("/login", data={
            "username": user.username,
            "password": "password123",
        })
        # form 再表示 (CAPTCHA エラー)
        assert resp.status_code == 200

    def test_login_invalid_captcha(self, client, user, captcha_enabled):
        with patch("app.views.auth.verify_captcha_token") as mock_v:
            mock_v.return_value = False
            resp = client.post("/login", data={
                "username": user.username,
                "password": "password123",
                "h-captcha-response": "fake-token",
            })
            assert resp.status_code == 200

    def test_login_valid_captcha(self, client, user, captcha_enabled):
        with patch("app.views.auth.verify_captcha_token") as mock_v:
            mock_v.return_value = True
            resp = client.post("/login", data={
                "username": user.username,
                "password": "password123",
                "h-captcha-response": "valid-token",
            })
            assert resp.status_code in (302, 303)


class TestLoginAuditorCaptcha:
    def test_login_auditor_invalid_captcha(self, client, auditor, captcha_enabled):
        with patch("app.views.auth.verify_captcha_token") as mock_v:
            mock_v.return_value = False
            resp = client.post("/login/auditor", data={
                "username": auditor.username,
                "password": "password123",
                "h-captcha-response": "x",
            })
            assert resp.status_code == 200


class TestRegisterCaptcha:
    def test_register_invalid_captcha(self, client, account_types, captcha_enabled):
        with patch("app.views.auth.verify_captcha_token") as mock_v:
            mock_v.return_value = False
            resp = client.post("/register", data={
                "username": "new",
                "email": "new@example.com",
                "password": "newpass",
                "password_confirm": "newpass",
                "h-captcha-response": "x",
            })
            # CAPTCHA フェイルで form 再表示
            assert resp.status_code == 200
            assert User.query.filter_by(username="new").first() is None


class TestRegisterAuditorCaptcha:
    def test_register_auditor_invalid_captcha(self, client, captcha_enabled):
        with patch("app.views.auth.verify_captcha_token") as mock_v:
            mock_v.return_value = False
            resp = client.post("/register/auditor", data={
                "username": "newaud",
                "email": "a@example.com",
                "password": "p",
                "password_confirm": "p",
                "h-captcha-response": "x",
            })
            assert resp.status_code == 200
            assert User.query.filter_by(username="newaud").first() is None
