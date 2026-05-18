"""ユーザー別AI API設定"""

from datetime import datetime, timezone

from app.extensions import db


class UserAIConfig(db.Model):
    """ユーザーごとのAI API設定"""

    __tablename__ = "user_ai_configs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True
    )
    provider = db.Column(db.String(20), nullable=False, default="openai")
    api_key_encrypted = db.Column(db.LargeBinary, nullable=False)
    model_name = db.Column(db.String(100), nullable=False, default="")
    custom_prompt = db.Column(db.Text, nullable=False, default="")
    compliance_check = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship(
        "User", backref=db.backref("ai_config", uselist=False, lazy="select")
    )

    def __repr__(self):
        return f"<UserAIConfig user={self.user_id} provider={self.provider}>"
