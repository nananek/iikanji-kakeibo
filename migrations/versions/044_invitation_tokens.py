"""招待トークンテーブルを追加 (Phase 8 #72)

公開タイミングコントロール (招待制ベータ) のため `invitation_tokens`
テーブルを新設。管理者が `flask invite-create <email>` で発行、token
は SHA-256 ハッシュで保存し、メール送信時のみ raw 値が露出する。

`REGISTRATION_INVITE_ONLY=true` のとき `auth.register` / `auth.register_auditor`
はトークン必須となり、メールアドレスとトークンの一致を検証する。

Revision ID: 044_invitation_tokens
Revises: 043_user_quota_warning_level
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "044_invitation_tokens"
down_revision = "043_user_quota_warning_level"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "invitation_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "issued_by", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_type", sa.String(20), nullable=False, server_default="personal",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "used_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "used_by", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("token_hash", name="uq_invitation_tokens_token_hash"),
    )


def downgrade():
    op.drop_table("invitation_tokens")
