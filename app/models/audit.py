from datetime import datetime, timezone

from app.extensions import db


class AuditGrant(db.Model):
    """監査用アカウントへのアクセス付与"""

    __tablename__ = "audit_grants"

    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    auditor_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    permission_level = db.Column(db.Integer, nullable=False)  # 1/2/3
    status = db.Column(db.String(10), nullable=False, default="draft")
    submitted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    owner = db.relationship(
        "User", foreign_keys=[owner_user_id], backref="audit_grants_given"
    )
    auditor = db.relationship(
        "User", foreign_keys=[auditor_user_id], backref="audit_grants_received"
    )
    grant_accounts = db.relationship(
        "AuditGrantAccount", backref="audit_grant", cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.UniqueConstraint(
            "owner_user_id", "auditor_user_id", name="uq_audit_grant_owner_auditor"
        ),
    )

    def __repr__(self):
        return f"<AuditGrant owner={self.owner_user_id} auditor={self.auditor_user_id} lv={self.permission_level}>"


class AuditGrantAccount(db.Model):
    """Lv2監査で公開する科目"""

    __tablename__ = "audit_grant_accounts"

    id = db.Column(db.Integer, primary_key=True)
    audit_grant_id = db.Column(
        db.Integer,
        db.ForeignKey("audit_grants.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_user_id = db.Column(db.Integer, nullable=False)
    account_code = db.Column(db.String(10), nullable=False)

    account = db.relationship("Account", foreign_keys=[account_user_id, account_code])

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["account_user_id", "account_code"],
            ["accounts.user_id", "accounts.code"],
            name="fk_aga_account",
        ),
        db.UniqueConstraint(
            "audit_grant_id", "account_code", name="uq_audit_grant_account"
        ),
    )
