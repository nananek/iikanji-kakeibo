"""パスキー専用モードとリカバリコード用カラムを users に追加

Revision ID: 034_passkey_only
Revises: 033_oauth_readonly
Create Date: 2026-05-16
"""

from alembic import op
import sqlalchemy as sa


revision = "034_passkey_only"
down_revision = "033_oauth_readonly"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "passkey_only_login",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("recovery_code_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("recovery_code_prefix", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("recovery_code_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("recovery_code_used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("users", "recovery_code_used_at")
    op.drop_column("users", "recovery_code_created_at")
    op.drop_column("users", "recovery_code_prefix")
    op.drop_column("users", "recovery_code_hash")
    op.drop_column("users", "passkey_only_login")
