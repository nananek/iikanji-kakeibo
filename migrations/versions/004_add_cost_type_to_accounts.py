"""Add cost_type to accounts

Revision ID: 004_add_cost_type
Revises: 003_user_ai_configs
Create Date: 2026-02-14
"""
from alembic import op
import sqlalchemy as sa

revision = "004_add_cost_type"
down_revision = "003_user_ai_configs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "accounts",
        sa.Column("cost_type", sa.String(10), nullable=True),
    )


def downgrade():
    op.drop_column("accounts", "cost_type")
