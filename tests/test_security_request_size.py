"""リクエストボディサイズ上限 (MAX_CONTENT_LENGTH) のテスト。

上限未設定だと巨大アップロードでメモリを枯渇させられる (DoS)。
"""

from app import create_app
from app.config import Config


class ProdLikeConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


def test_max_content_length_is_configured():
    """本番相当の設定でリクエストサイズ上限が設定されていること。"""
    app = create_app(ProdLikeConfig)
    limit = app.config.get("MAX_CONTENT_LENGTH")
    assert limit is not None
    assert limit > 0


def test_oversized_request_rejected_with_413():
    """上限を超えるボディは 413 で拒否されること。"""
    class SmallLimitConfig(ProdLikeConfig):
        MAX_CONTENT_LENGTH = 1024

    app = create_app(SmallLimitConfig)
    client = app.test_client()
    resp = client.post("/login", data={"username": "a" * 5000, "password": "x"})
    assert resp.status_code == 413
