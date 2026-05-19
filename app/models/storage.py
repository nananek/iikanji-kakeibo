"""ストレージ使用量集計テーブル (Phase 5 #70)。

各ユーザー 1 行で証憑画像の総使用バイト数を持つ。アップロード時に
`record_upload`、削除時に `record_delete` で増減する。
"""

from datetime import datetime, timezone

from app.extensions import db


class StorageUsage(db.Model):
    __tablename__ = "storage_usage"

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        primary_key=True,
    )
    used_bytes = db.Column(
        db.BigInteger,
        nullable=False,
        default=0,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self):
        return (
            f"<StorageUsage user_id={self.user_id} "
            f"used_bytes={self.used_bytes}>"
        )
