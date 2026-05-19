from datetime import datetime, timezone

from app.extensions import db


class VoucherAuditLog(db.Model):
    """証憑操作ログ（電帳法 改ざん防止）。

    電帳法スキャナ保存の 7 年保管義務に従い、Voucher 物理削除・ユーザー
    退会後も匿名化して保持する。`voucher_id` / `user_id` は nullable
    + ondelete=SET NULL (Phase 5 / Phase 4 マイグレーション 042 で対応)。
    """

    __tablename__ = "voucher_audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(
        db.Integer,
        db.ForeignKey("vouchers.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action = db.Column(db.String(50), nullable=False)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    voucher = db.relationship("Voucher", backref=db.backref("audit_logs", lazy="dynamic"))
    user = db.relationship("User")
