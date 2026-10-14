"""Host ヘッダ偽装に対する外部 URL 生成の回帰テスト。

SERVER_NAME を設定すると url_for(_external=True) は Host ヘッダでは
なく SERVER_NAME を使う。公開運用では SERVER_NAME 設定が必須。
"""

from app import create_app
from app.config import Config


class HostConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    SERVER_NAME = "kakeibo.example.com"
    PREFERRED_URL_SCHEME = "https"


def test_external_url_ignores_host_header():
    app = create_app(HostConfig)
    with app.app_context():
        from app.extensions import db
        db.create_all()
        c = app.test_client()
        resp = c.post(
            "/oauth/device",
            json={"client_name": "x"},
            headers={"Host": "evil.example.com"},
        )
        assert resp.status_code == 200
        uri = resp.get_json()["verification_uri"]
        assert uri.startswith("https://kakeibo.example.com/")
        assert "evil.example.com" not in uri
        db.drop_all()
