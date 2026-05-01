"""OAuth Device Authorization Grant 用テーブルを追加

Revision ID: 032_oauth_device_flow
Revises: 031_tax_mapping_multi_form
Create Date: 2026-05-01
"""

from alembic import op
import sqlalchemy as sa


revision = "032_oauth_device_flow"
down_revision = "031_tax_mapping_multi_form"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "oauth_devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("device_code_hash", sa.String(64), nullable=False),
        sa.Column("user_code", sa.String(16), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("client_name", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_oauth_devices_device_code_hash",
        "oauth_devices",
        ["device_code_hash"],
        unique=True,
    )
    op.create_index(
        "ix_oauth_devices_user_code",
        "oauth_devices",
        ["user_code"],
        unique=True,
    )

    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_oauth_tokens_token_hash",
        "oauth_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_oauth_tokens_user_id",
        "oauth_tokens",
        ["user_id"],
    )


def downgrade():
    op.drop_index("ix_oauth_tokens_user_id", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_token_hash", table_name="oauth_tokens")
    op.drop_table("oauth_tokens")
    op.drop_index("ix_oauth_devices_user_code", table_name="oauth_devices")
    op.drop_index("ix_oauth_devices_device_code_hash", table_name="oauth_devices")
    op.drop_table("oauth_devices")
