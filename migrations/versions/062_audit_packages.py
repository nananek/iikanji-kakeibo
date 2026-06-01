"""E2EE Phase E5 (#112) PR-B: 監査連携テーブル (audit_packages / audit_responses)

HPKE 非同期ワークフロー方式の監査 (設計書 §14) で使うテーブルを新設する。

- audit_packages:  owner → auditor へ HPKE 暗号化して渡すスナップショット
- audit_responses: auditor → owner へ返す修正案 / 差戻し (HPKE 逆方向)
- audit_grants.revoked_at: 監査依頼の論理失効 (§14.10)

本 PR はスキーマ + モデルのみ。API / 暗号化フローは PR-C 以降。

Revision ID: 062_audit_packages
Revises: 061_users_x25519_private_key
Create Date: 2026-06-02
"""

from alembic import op
import sqlalchemy as sa


revision = "062_audit_packages"
down_revision = "061_users_x25519_private_key"
branch_labels = None
depends_on = None


def upgrade():
    # audit_grants.revoked_at (論理失効, §14.10)
    with op.batch_alter_table("audit_grants") as batch_op:
        batch_op.add_column(
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True)
        )

    # audit_packages (§14.1)
    op.create_table(
        "audit_packages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("audit_grant_id", sa.Integer(), nullable=False),
        sa.Column("round_id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("auditor_user_id", sa.Integer(), nullable=False),
        sa.Column("permission_level", sa.SmallInteger(), nullable=False),
        sa.Column("ephemeral_pubkey", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("snapshot_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["audit_grant_id"], ["audit_grants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["auditor_user_id"], ["users.id"]),
        sa.UniqueConstraint(
            "audit_grant_id", "round_id", name="uq_audit_package_grant_round"
        ),
    )
    op.create_index(
        "ix_audit_packages_owner_user_id", "audit_packages", ["owner_user_id"]
    )
    op.create_index(
        "ix_audit_packages_auditor_user_id", "audit_packages", ["auditor_user_id"]
    )
    op.create_index(
        "ix_audit_packages_expires_at", "audit_packages", ["expires_at"]
    )

    # audit_responses (§14.2)
    op.create_table(
        "audit_responses",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("audit_package_id", sa.BigInteger(), nullable=False),
        sa.Column("response_type", sa.String(length=10), nullable=False),
        sa.Column("ephemeral_pubkey", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["audit_package_id"], ["audit_packages.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "response_type IN ('revision', 'rejection')",
            name="ck_audit_response_type",
        ),
    )
    op.create_index(
        "ix_audit_responses_audit_package_id",
        "audit_responses",
        ["audit_package_id"],
    )
    op.create_index(
        "ix_audit_responses_expires_at", "audit_responses", ["expires_at"]
    )


def downgrade():
    op.drop_index("ix_audit_responses_expires_at", table_name="audit_responses")
    op.drop_index(
        "ix_audit_responses_audit_package_id", table_name="audit_responses"
    )
    op.drop_table("audit_responses")

    op.drop_index("ix_audit_packages_expires_at", table_name="audit_packages")
    op.drop_index(
        "ix_audit_packages_auditor_user_id", table_name="audit_packages"
    )
    op.drop_index("ix_audit_packages_owner_user_id", table_name="audit_packages")
    op.drop_table("audit_packages")

    with op.batch_alter_table("audit_grants") as batch_op:
        batch_op.drop_column("revoked_at")
