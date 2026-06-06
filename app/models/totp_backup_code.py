from datetime import datetime, timezone

from app.extensions import db


class TotpBackupCode(db.Model):
    """TOTP 2FA のバックアップコード (#385 PR-T1、設計書 §3.6.3)。

    TOTP デバイス紛失時のワンタイムコード。`recovery_code` と同方式で平文は保存せず
    SHA-256 ハッシュのみ保管し、1 回限り使用 (`used_at` でマーク) する。
    """

    __tablename__ = "totp_backup_codes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash = db.Column(db.String(64), nullable=False)   # SHA-256 hexdigest
    code_prefix = db.Column(db.String(20), nullable=True)  # 表示用 (先頭数桁 + "...")
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship(
        "User", backref=db.backref("totp_backup_codes", lazy="dynamic")
    )

    def __repr__(self):
        return f"<TotpBackupCode {self.id} user={self.user_id} used={self.used_at is not None}>"
