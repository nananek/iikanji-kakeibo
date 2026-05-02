"""CAPTCHA サービス (services/captcha.py) のテスト"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.captcha import (
    get_captcha_response_field,
    is_captcha_enabled,
    verify_captcha_token,
)


class TestIsCaptchaEnabled:
    def test_disabled_when_no_provider(self, app):
        with app.test_request_context():
            with patch.object(app.config, "get", side_effect=lambda k, *a: None):
                assert is_captcha_enabled() is False

    def test_enabled_for_known_provider(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "hcaptcha"
            try:
                assert is_captcha_enabled() is True
            finally:
                app.config["CAPTCHA_PROVIDER"] = None

    def test_disabled_for_unknown_provider(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "unknown"
            try:
                assert is_captcha_enabled() is False
            finally:
                app.config["CAPTCHA_PROVIDER"] = None


class TestGetCaptchaResponseField:
    def test_no_provider(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = None
            assert get_captcha_response_field() is None

    def test_hcaptcha_field(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "hcaptcha"
            try:
                assert get_captcha_response_field() == "h-captcha-response"
            finally:
                app.config["CAPTCHA_PROVIDER"] = None

    def test_recaptcha_field(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "recaptcha"
            try:
                assert get_captcha_response_field() == "g-recaptcha-response"
            finally:
                app.config["CAPTCHA_PROVIDER"] = None

    def test_turnstile_field(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "turnstile"
            try:
                assert get_captcha_response_field() == "cf-turnstile-response"
            finally:
                app.config["CAPTCHA_PROVIDER"] = None

    def test_unknown_provider(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "BAD"
            try:
                assert get_captcha_response_field() is None
            finally:
                app.config["CAPTCHA_PROVIDER"] = None


class TestVerifyCaptchaToken:
    def test_disabled_passes(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = None
            app.config["CAPTCHA_SECRET_KEY"] = None
            assert verify_captcha_token("any") is True

    def test_unknown_provider_fails(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "BAD"
            app.config["CAPTCHA_SECRET_KEY"] = "secret"
            try:
                assert verify_captcha_token("token") is False
            finally:
                app.config["CAPTCHA_PROVIDER"] = None
                app.config["CAPTCHA_SECRET_KEY"] = None

    def test_hcaptcha_success(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "hcaptcha"
            app.config["CAPTCHA_SECRET_KEY"] = "secret"
            try:
                with patch("app.services.captcha.httpx.post") as mock_post:
                    resp = MagicMock()
                    resp.json.return_value = {"success": True}
                    mock_post.return_value = resp
                    assert verify_captcha_token("token") is True
            finally:
                app.config["CAPTCHA_PROVIDER"] = None
                app.config["CAPTCHA_SECRET_KEY"] = None

    def test_recaptcha_failure(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "recaptcha"
            app.config["CAPTCHA_SECRET_KEY"] = "secret"
            try:
                with patch("app.services.captcha.httpx.post") as mock_post:
                    resp = MagicMock()
                    resp.json.return_value = {"success": False}
                    mock_post.return_value = resp
                    assert verify_captcha_token("token") is False
            finally:
                app.config["CAPTCHA_PROVIDER"] = None
                app.config["CAPTCHA_SECRET_KEY"] = None

    def test_mcaptcha_no_url(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "mcaptcha"
            app.config["CAPTCHA_SECRET_KEY"] = "secret"
            app.config["CAPTCHA_API_URL"] = None
            try:
                assert verify_captcha_token("t") is False
            finally:
                app.config["CAPTCHA_PROVIDER"] = None
                app.config["CAPTCHA_SECRET_KEY"] = None

    def test_mcaptcha_with_url(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "mcaptcha"
            app.config["CAPTCHA_SECRET_KEY"] = "secret"
            app.config["CAPTCHA_API_URL"] = "https://mcap.example/api"
            try:
                with patch("app.services.captcha.httpx.post") as mock_post:
                    resp = MagicMock()
                    resp.json.return_value = {"success": True}
                    mock_post.return_value = resp
                    assert verify_captcha_token("t") is True
                    assert "mcap.example" in mock_post.call_args.args[0]
            finally:
                app.config["CAPTCHA_PROVIDER"] = None
                app.config["CAPTCHA_SECRET_KEY"] = None
                app.config["CAPTCHA_API_URL"] = None

    def test_http_error(self, app):
        with app.test_request_context():
            app.config["CAPTCHA_PROVIDER"] = "hcaptcha"
            app.config["CAPTCHA_SECRET_KEY"] = "secret"
            try:
                with patch("app.services.captcha.httpx.post") as mock_post:
                    mock_post.side_effect = Exception("network down")
                    assert verify_captcha_token("t") is False
            finally:
                app.config["CAPTCHA_PROVIDER"] = None
                app.config["CAPTCHA_SECRET_KEY"] = None
