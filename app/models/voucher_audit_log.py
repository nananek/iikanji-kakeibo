from datetime import datetime, timezone

from app.extensions import db


class VoucherAuditLog(db.Model):
    """証憑操作ログ（電帳法 改ざん防止）"""

    __tablename__ = "voucher_audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(
        db.Integer, db.ForeignKey("vouchers.id"), nullable=False, index=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    action = db.Column(db.String(50), nullable=False)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    voucher = db.relationship("Voucher", backref=db.backref("audit_logs", lazy="dynamic"))
    user = db.relationship("User")
