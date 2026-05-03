import hashlib
import secrets
from datetime import datetime, timezone

from app.extensions import db

KEY_PREFIX = "ik_"

ALL_SCOPES = (
    "journals:create", "journals:read", "journals:delete",
    "ai:analyze", "reports:read",
)
SCOPE_LABELS = {
    "journals:create": "仕訳起票",
    "journals:read": "仕訳閲覧",
    "journals:delete": "仕訳削除",
    "ai:analyze": "AI証憑仕訳",
    "reports:read": "レポート閲覧",
}
SCOPE_DEPENDENCIES = {"journals:delete": "journals:read"}


class APIKey(db.Model):
    """外部 API 認証キー"""

    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    key_hash = db.Column(db.String(64), nullable=False, unique=True)
    key_prefix = db.Column(db.String(20), nullable=False)
    scopes = db.Column(db.String(200), nullable=False, default="journals:create")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    last_used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("api_keys", lazy="dynamic"))

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes.split(",")

    @property
    def scope_list(self) -> list[str]:
        return [s for s in self.scopes.split(",") if s]

    @staticmethod
    def generate():
        """新しい API キーを生成し (raw_key, key_hash, key_prefix) を返す"""
        raw = KEY_PREFIX + secrets.token_hex(32)
        h = hashlib.sha256(raw.encode()).hexdigest()
        prefix = raw[:10] + "..."
        return raw, h, prefix

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()
