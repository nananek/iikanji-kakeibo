from datetime import datetime, timezone

from app.extensions import db


class Voucher(db.Model):
    """証憑（電帳法対応の永続保存）"""

    __tablename__ = "vouchers"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    journal_entry_id = db.Column(
        db.Integer,
        db.ForeignKey("journal_entries.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    image_key = db.Column(db.String(255), nullable=False)
    image_mime = db.Column(db.String(50), nullable=False)
    original_filename = db.Column(db.String(255), nullable=True)
    file_hash = db.Column(db.String(64), nullable=True)  # SHA-256
    uploaded_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    user = db.relationship("User", backref=db.backref("vouchers", lazy="dynamic"))
    journal_entry = db.relationship(
        "JournalEntry", backref=db.backref("vouchers", lazy="select")
    )
