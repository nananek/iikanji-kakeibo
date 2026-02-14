"""Add system_role to accounts table

Revision ID: 010_system_role
Revises: 009_deactivated_year
Create Date: 2026-02-15
"""
from alembic import op
import sqlalchemy as sa

revision = "010_system_role"
down_revision = "009_deactivated_year"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "accounts",
        sa.Column("system_role", sa.String(30), nullable=True),
    )
    # 既存データ: tax_category='proprietor' → system_role='proprietor' に移行
    op.execute(
        "UPDATE accounts SET system_role = 'proprietor' WHERE tax_category = 'proprietor'"
    )
    # 既存データ: code='3020' の繰越利益に system_role 設定
    op.execute(
        "UPDATE accounts SET system_role = 'retained_earnings' WHERE code = '3020'"
    )


def downgrade():
    op.drop_column("accounts", "system_role")
