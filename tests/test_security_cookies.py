"""セッション/永続ログイン Cookie のセキュリティ属性テスト。

公開 (HTTPS) 運用では Set-Cookie に Secure 属性が必要。SameSite も
明示する。HttpOnly は既存挙動の回帰ガード。
"""

from app import create_app
from app.config import Config
from app.extensions import db as _db


class HttpsConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    PREFERRED_URL_SCHEME = "https"


def _login_and_cookies(config):
    app = create_app(config)
    app.debug = False
    with app.app_context():
        _db.create_all()
        from app.models.user import User
        u = User(username="cookietest", email="c@example.com",
                 user_type="personal")
        u.set_password("password123")
        _db.session.add(u)
        _db.session.commit()
        c = app.test_client()
        resp = c.post("/login", data={"username": "cookietest",
                                      "password": "password123"})
        cookies = resp.headers.getlist("Set-Cookie")
        _db.drop_all()
        return cookies


class TestCookieAttributes:
    def test_session_cookie_secure_on_https(self):
        """HTTPS 運用時、セッション Cookie に Secure が付く。"""
        cookies = _login_and_cookies(HttpsConfig)
        session_cookie = next(
            (c for c in cookies if c.startswith("session=")), None
        )
        assert session_cookie is not None
        assert "Secure" in session_cookie

    def test_session_cookie_httponly_and_samesite(self):
        """セッション Cookie に HttpOnly と SameSite が付く。"""
        cookies = _login_and_cookies(HttpsConfig)
        session_cookie = next(
            (c for c in cookies if c.startswith("session=")), None
        )
        assert session_cookie is not None
        assert "HttpOnly" in session_cookie
        assert "SameSite=Lax" in session_cookie

    def test_remember_cookie_secure_on_https(self):
        """永続ログイン (remember_token) にも Secure が付く。"""
        cookies = _login_and_cookies(HttpsConfig)
        remember_cookie = next(
            (c for c in cookies if c.startswith("remember_token=")), None
        )
        assert remember_cookie is not None
        assert "Secure" in remember_cookie
