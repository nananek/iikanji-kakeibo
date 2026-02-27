"""セキュリティヘッダーテスト — after_request フックで設定されるヘッダーの検証"""

import pytest

from app import create_app
from app.config import Config
from app.extensions import db as _db


class TestSecurityHeaders:
    """全レスポンスにセキュリティヘッダーが設定されることを検証"""

    def test_x_content_type_options(self, db, logged_in_client):
        resp = logged_in_client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self, db, logged_in_client):
        resp = logged_in_client.get("/")
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"

    def test_referrer_policy(self, db, logged_in_client):
        resp = logged_in_client.get("/")
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_headers_on_unauthenticated_page(self, client):
        """未認証ページ（ログインページ）にもヘッダーが付く"""
        resp = client.get("/login")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"

    def test_headers_on_api_response(self, client, db, user, accounts, auth_header):
        """API JSON レスポンスにもヘッダーが付く"""
        resp = client.get("/api/v1/journals", headers=auth_header)
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


class TestHSTS:
    """HSTS ヘッダーの debug モード条件分岐"""

    def test_hsts_absent_in_debug(self, client):
        """debug=True のとき HSTS ヘッダーが付かない"""
        resp = client.get("/login")
        assert "Strict-Transport-Security" not in resp.headers

    def test_hsts_present_in_production(self):
        """debug=False のとき HSTS ヘッダーが付く"""
        class ProdLikeConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            WTF_CSRF_ENABLED = False
            RATELIMIT_ENABLED = False

        app = create_app(ProdLikeConfig)
        app.debug = False
        with app.app_context():
            _db.create_all()
            c = app.test_client()
            resp = c.get("/login")
            assert "Strict-Transport-Security" in resp.headers
            hsts = resp.headers["Strict-Transport-Security"]
            assert "max-age=31536000" in hsts
            assert "includeSubDomains" in hsts
            _db.drop_all()
