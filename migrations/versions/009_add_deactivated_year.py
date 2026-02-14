"""Add deactivated_year to accounts table

Revision ID: 009_deactivated_year
Revises: 008_audit_accounts
Create Date: 2026-02-15
"""
from alembic import op
import sqlalchemy as sa

revision = "009_deactivated_year"
down_revision = "008_audit_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "accounts",
        sa.Column("deactivated_year", sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_column("accounts", "deactivated_year")
