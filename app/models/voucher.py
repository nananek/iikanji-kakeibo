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
    # 容量計上 (Phase 5 #70) のためのファイルサイズ (バイト)。新規作成時に
    # セット。既存 Voucher は NULL のままで、整合性監査バッチで埋める想定。
    file_size = db.Column(db.BigInteger, nullable=True)
    uploaded_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    # 論理削除 (Phase 5 #70 / 電帳法証跡永続化): 削除時にタイムスタンプを
    # セットし、Voucher row 自体は DB に残す。これにより `VoucherAuditLog`
    # の `action="deleted"` を FK RESTRICT 下でも永続化できる。
    # 一覧・検索・REST API は `Voucher.active()` (= deleted_at IS NULL)
    # で透過的にフィルタする。
    deleted_at = db.Column(
        db.DateTime(timezone=True), nullable=True, index=True,
    )

    user = db.relationship("User", backref=db.backref("vouchers", lazy="dynamic"))
    # journal_entry.vouchers backref は削除済も含む全件を返す。UI/API では
    # `entry.active_vouchers` プロパティ (JournalEntry 側で定義) を使い、
    # 全件 (削除済含む) が必要な log_voucher_orphan / api_voucher_logs
    # などでは `entry.vouchers` を直接使うこと。
    journal_entry = db.relationship(
        "JournalEntry", backref=db.backref("vouchers", lazy="select")
    )

    @classmethod
    def active(cls):
        """論理削除されていない Voucher のみを返す query."""
        return cls.query.filter(cls.deleted_at.is_(None))

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
