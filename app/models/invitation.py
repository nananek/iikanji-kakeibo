"""招待トークン (Phase 8 #72): 公開タイミングコントロール用."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.extensions import db


class InvitationToken(db.Model):
    """register / register_auditor で要求する 1 回限り使用の招待トークン。

    raw トークンは `generate()` 時に 1 度だけ返り、DB には SHA-256 ハッシュ
    のみ保存する (APIKey と同じパターン)。`REGISTRATION_INVITE_ONLY=true`
    の環境では register view がトークン必須化される。
    """

    __tablename__ = "invitation_tokens"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    issued_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # personal / auditor — register / register_auditor を分岐させる用途
    user_type = db.Column(db.String(20), nullable=False, default="personal")
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    used_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    @classmethod
    def generate(
        cls, email: str, *,
        issued_by_id: int | None = None,
        user_type: str = "personal",
        expires_in_days: int = 7,
    ):
        """raw トークンと永続化前の InvitationToken を返す。

        raw トークンは戻り値経由でしか取得できない (DB にはハッシュのみ)。
        呼出側で `db.session.add(record)` + `commit()` する。
        """
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        record = cls(
            email=email,
            token_hash=token_hash,
            issued_by=issued_by_id,
            user_type=user_type,
            expires_at=datetime.now(timezone.utc) + timedelta(
                days=expires_in_days
            ),
        )
        return raw, record

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def find_valid(cls, raw: str):
        """raw トークンで検証し、有効な未使用レコードを返す (なければ None)."""
        if not raw:
            return None
        record = cls.query.filter_by(token_hash=cls.hash_token(raw)).first()
        if record is None or not record.is_valid():
            return None
        return record

    def is_valid(self) -> bool:
        """未使用かつ期限内であることを判定."""
        if self.used_at is not None:
            return False
        now = datetime.now(timezone.utc)
        # SQLite では expires_at が timezone-naive で返ることがあるため正規化
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < now:
            return False
        return True

    def mark_used(self, user_id: int) -> None:
        """登録成功時に「使用済」マークする。呼出側で commit すること。"""
        self.used_at = datetime.now(timezone.utc)
        self.used_by = user_id
