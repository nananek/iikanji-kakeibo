"""OAuth 2.0 Device Authorization Grant (RFC 8628) モデル"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.extensions import db

# device_code / user_code の有効時間
DEVICE_CODE_EXPIRES_IN = 600  # 10分
# クライアントが /oauth/token をポーリングする推奨間隔 (秒)
DEVICE_CODE_POLL_INTERVAL = 5

# user_code に使用する文字（混同を避けるため I, O, 0, 1 を除外）
_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

OAUTH_TOKEN_PREFIX = "ikt_"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class OAuthDevice(db.Model):
    """Device Authorization Grant のセッション記録"""

    __tablename__ = "oauth_devices"

    id = db.Column(db.Integer, primary_key=True)
    device_code_hash = db.Column(
        db.String(64), nullable=False, unique=True, index=True
    )
    user_code = db.Column(db.String(16), nullable=False, unique=True, index=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    status = db.Column(
        db.String(20), nullable=False, default="pending"
    )  # pending / approved / denied / expired / consumed
    client_name = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now_utc)
    expires_at = db.Column(db.DateTime, nullable=False)
    last_polled_at = db.Column(db.DateTime, nullable=True)
    read_only = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship(
        "User", backref=db.backref("oauth_devices", lazy="dynamic")
    )

    @staticmethod
    def generate_user_code() -> str:
        """ユーザー入力用の8文字コード（XXXX-XXXX形式）を生成"""
        chars = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
        return f"{chars[:4]}-{chars[4:]}"

    @staticmethod
    def generate_device_code() -> str:
        """device_code (raw) を生成"""
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash_device_code(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def create_pending(cls, client_name: str | None = None):
        """新しい pending セッションを作成し (raw_device_code, OAuthDevice) を返す"""
        for _ in range(8):
            user_code = cls.generate_user_code()
            existing = cls.query.filter_by(user_code=user_code).first()
            if not existing:
                break
        else:
            raise RuntimeError("user_code の生成に失敗しました")

        raw_device = cls.generate_device_code()
        device = cls(
            device_code_hash=cls.hash_device_code(raw_device),
            user_code=user_code,
            status="pending",
            client_name=(client_name or "")[:100] or None,
            expires_at=_now_utc() + timedelta(seconds=DEVICE_CODE_EXPIRES_IN),
        )
        return raw_device, device

    def is_expired(self) -> bool:
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return _now_utc() >= expires


class OAuthToken(db.Model):
    """OAuth Device Flow で発行されたアクセストークン"""

    __tablename__ = "oauth_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    token_prefix = db.Column(db.String(20), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now_utc)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    read_only = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship(
        "User", backref=db.backref("oauth_tokens", lazy="dynamic")
    )

    @staticmethod
    def generate():
        """アクセストークンを生成し (raw, hash, prefix) を返す"""
        raw = OAUTH_TOKEN_PREFIX + secrets.token_hex(32)
        h = hashlib.sha256(raw.encode()).hexdigest()
        prefix = raw[:11] + "..."
        return raw, h, prefix

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()
