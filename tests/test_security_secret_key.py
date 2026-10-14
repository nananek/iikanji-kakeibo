"""SECRET_KEY の本番 fail-fast テスト。

既定値 (dev-secret-key 等) や空のまま本番起動すると、セッション・
CSRF・remember-me トークンが偽造可能になるため、起動時に拒否する。
"""

import pytest

from app import create_app
from app.config import Config


class InsecureSecretConfig(Config):
    TESTING = False
    SECRET_KEY = "dev-secret-key"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


class EmptySecretConfig(Config):
    TESTING = False
    SECRET_KEY = ""
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


class StrongSecretConfig(Config):
    TESTING = False
    SECRET_KEY = "Xq7" * 20  # 本番相当でも通る強い鍵
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


class TestSecretKeyGuard:
    def test_default_secret_rejected_in_production(self):
        with pytest.raises(RuntimeError):
            create_app(InsecureSecretConfig)

    def test_empty_secret_rejected_in_production(self):
        with pytest.raises(RuntimeError):
            create_app(EmptySecretConfig)

    def test_strong_secret_accepted(self):
        app = create_app(StrongSecretConfig)
        assert app.config["SECRET_KEY"] == "Xq7" * 20
