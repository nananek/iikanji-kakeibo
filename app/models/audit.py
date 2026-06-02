from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, Integer
from sqlalchemy.orm import validates

from app.extensions import db


# bigserial PK は SQLite では autoincrement しないため、テスト (SQLite) では
# INTEGER PRIMARY KEY にフォールバックさせる。PostgreSQL では BIGINT のまま。
# _BigIntPK = bigserial PK 用、_BigInt = それを参照する FK 用 (型は同一だが
# 意図を区別するため別名にする)。
_BigIntPK = BigInteger().with_variant(Integer, "sqlite")
_BigInt = BigInteger().with_variant(Integer, "sqlite")


# 監査パッケージ / 修正案の 90 日 TTL (設計書 §14.8)。expires_at の既定計算に使う。
AUDIT_PACKAGE_TTL = timedelta(days=90)

RESPONSE_TYPE_REVISION = "revision"
RESPONSE_TYPE_REJECTION = "rejection"
ALLOWED_RESPONSE_TYPES = (RESPONSE_TYPE_REVISION, RESPONSE_TYPE_REJECTION)


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
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    # 監査依頼の論理失効 (E5 #112, 設計書 §14.10)。NULL=有効。失効後は新規
    # AuditPackage の作成を拒否し、既存パッケージは 90 日 TTL で自動消滅させる
    # (MK ローテーション不要 = ワークフロー方式では auditor が MK を持たないため)。
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    owner = db.relationship(
        "User", foreign_keys=[owner_user_id], backref="audit_grants_given"
    )
    auditor = db.relationship(
        "User", foreign_keys=[auditor_user_id], backref="audit_grants_received"
    )
    grant_accounts = db.relationship(
        "AuditGrantAccount", backref="audit_grant", cascade="all, delete-orphan"
    )
    packages = db.relationship(
        "AuditPackage", backref="audit_grant", cascade="all, delete-orphan"
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


def _default_expires_at():
    return datetime.now(timezone.utc) + AUDIT_PACKAGE_TTL


class AuditPackage(db.Model):
    """owner → auditor へ HPKE 暗号化して渡す監査スナップショット (設計書 §14.1)。

    owner がローカルで MK 復号した仕訳/残高/画像スナップショットを auditor の
    X25519 公開鍵で HPKE 暗号化 (ephemeral X25519 + AES-256-GCM) して保管する。
    サーバは ciphertext を預かるだけで平文を持たない。`(audit_grant_id, round_id)`
    でマルチラウンドを管理し、最新 round のみ作業可 (§14.7)。
    """

    __tablename__ = "audit_packages"

    id = db.Column(_BigIntPK, primary_key=True)
    audit_grant_id = db.Column(
        db.Integer,
        db.ForeignKey("audit_grants.id", ondelete="CASCADE"),
        nullable=False,
    )
    round_id = db.Column(db.Integer, nullable=False)
    # owner / auditor を非正規化保持 (サーバ側 IDOR フィルタ用、§14.11)
    owner_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    auditor_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    permission_level = db.Column(db.SmallInteger, nullable=False)  # 1/2/3
    # HPKE encapsulated key (送信側 ephemeral X25519 公開鍵, 32B)
    ephemeral_pubkey = db.Column(db.LargeBinary, nullable=False)
    # HPKE 暗号文 (スナップショット JSON: 仕訳/残高/画像)
    ciphertext = db.Column(db.LargeBinary, nullable=False)
    # SHA-256(平文スナップショット) 改ざん検出用 (32B)
    snapshot_hash = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # created_at + 90 日 (§14.8)。`flask audit-cleanup` (後続 PR) が削除に使う。
    expires_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_default_expires_at
    )
    # owner が修正案を採用確定した時刻 (NULL=未対応, §14.2)。AuditResponse は作らない。
    owner_accepted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    owner = db.relationship("User", foreign_keys=[owner_user_id])
    auditor = db.relationship("User", foreign_keys=[auditor_user_id])
    responses = db.relationship(
        "AuditResponse", backref="audit_package", cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.UniqueConstraint(
            "audit_grant_id", "round_id", name="uq_audit_package_grant_round"
        ),
        db.CheckConstraint(
            "permission_level IN (1, 2, 3)",
            name="ck_audit_package_permission_level",
        ),
        db.Index("ix_audit_packages_owner_user_id", "owner_user_id"),
        db.Index("ix_audit_packages_auditor_user_id", "auditor_user_id"),
        db.Index("ix_audit_packages_expires_at", "expires_at"),
    )

    def __repr__(self):
        return (
            f"<AuditPackage id={self.id} grant={self.audit_grant_id} "
            f"round={self.round_id} lv={self.permission_level}>"
        )


class AuditResponse(db.Model):
    """auditor → owner へ HPKE 暗号化して返す修正案 / 差戻し (設計書 §14.2)。

    auditor が AuditPackage を自分の秘密鍵で復号 → 修正案 or 差戻し理由を owner の
    公開鍵で HPKE 暗号化して返す (DH の役割が逆方向)。採用は AuditResponse を作らず
    AuditPackage.owner_accepted_at で表す。
    """

    __tablename__ = "audit_responses"

    id = db.Column(_BigIntPK, primary_key=True)
    audit_package_id = db.Column(
        _BigInt,
        db.ForeignKey("audit_packages.id", ondelete="CASCADE"),
        nullable=False,
    )
    response_type = db.Column(db.String(10), nullable=False)  # revision / rejection
    ephemeral_pubkey = db.Column(db.LargeBinary, nullable=False)
    ciphertext = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_default_expires_at
    )
    owner_acknowledged_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.CheckConstraint(
            "response_type IN ('revision', 'rejection')",
            name="ck_audit_response_type",
        ),
        db.Index("ix_audit_responses_audit_package_id", "audit_package_id"),
        db.Index("ix_audit_responses_expires_at", "expires_at"),
    )

    @validates("response_type")
    def _validate_response_type(self, key, value):
        """DB CHECK 制約と同じルールをアプリ層 (SQLite テスト) でも強制。"""
        if value not in ALLOWED_RESPONSE_TYPES:
            raise ValueError(
                f"response_type must be one of {ALLOWED_RESPONSE_TYPES}, got {value!r}"
            )
        return value

    def __repr__(self):
        return (
            f"<AuditResponse id={self.id} package={self.audit_package_id} "
            f"type={self.response_type}>"
        )
