"""Add user_ai_configs table

Revision ID: 003_user_ai_configs
Revises: 002_webauthn_credentials
Create Date: 2026-02-14
"""
from alembic import op
import sqlalchemy as sa

revision = "003_user_ai_configs"
down_revision = "002_webauthn_credentials"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_ai_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "provider", sa.String(20), nullable=False, server_default="openai"
        ),
        sa.Column("api_key_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "model_name", sa.String(100), nullable=False, server_default=""
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_table("user_ai_configs")
