from datetime import datetime, timezone

from app.extensions import db


class AIDraft(db.Model):
    """AI証憑の一時保存"""

    __tablename__ = "ai_drafts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    image_data = db.Column(db.LargeBinary, nullable=False)
    image_mime = db.Column(db.String(50), nullable=False)
    comment = db.Column(db.String(500), default="")
    suggestions_json = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(20), nullable=False, default="analyzed"
    )  # analyzed / done
    discord_webhook_url = db.Column(db.String(500), nullable=True)
    discord_message_id = db.Column(db.String(30), nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("User", backref=db.backref("ai_drafts", lazy="dynamic"))
