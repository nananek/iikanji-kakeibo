"""Webhook 通知設定モデル。

Discord 等の外部 Webhook URL とイベント種別を保管する。account_deletion から
参照されるほか、Discord 通知設定 UI の再構築 (将来) で使用される。
"""

from datetime import datetime, timezone

from app.extensions import db


class WebhookConfig(db.Model):
    """Webhook 通知の設定"""

    __tablename__ = "webhook_configs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    provider = db.Column(db.String(20), nullable=False)  # "discord"
    webhook_url = db.Column(db.String(500), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    events_json = db.Column(
        db.Text, nullable=False, default='["import_success"]'
    )
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
        "User", backref=db.backref("webhook_configs", lazy="dynamic")
    )
