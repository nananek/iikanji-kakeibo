from datetime import datetime, timezone

from app.extensions import db


class AutoImportSource(db.Model):
    """外部ファイルソースの設定"""

    __tablename__ = "auto_import_sources"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    provider = db.Column(db.String(20), nullable=False)  # "webdav"
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    config_json = db.Column(db.Text, nullable=False)
    credentials_encrypted = db.Column(db.LargeBinary, nullable=False)
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
        "User", backref=db.backref("auto_import_sources", lazy="dynamic")
    )


class ProcessedFile(db.Model):
    """処理済みファイルの追跡（重複防止）"""

    __tablename__ = "processed_files"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("auto_import_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_path = db.Column(db.String(500), nullable=False)
    etag = db.Column(db.String(100), nullable=True)
    draft_id = db.Column(
        db.Integer, db.ForeignKey("ai_drafts.id"), nullable=True
    )
    status = db.Column(db.String(20), nullable=False, default="success")
    error_message = db.Column(db.Text, nullable=True)
    processed_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    source = db.relationship(
        "AutoImportSource",
        backref=db.backref("processed_files", lazy="dynamic", cascade="all, delete-orphan"),
    )
    draft = db.relationship("AIDraft")

    __table_args__ = (
        db.UniqueConstraint("source_id", "file_path", name="uq_source_file"),
    )


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
