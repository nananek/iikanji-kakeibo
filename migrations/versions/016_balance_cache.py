"""Add balance_caches table for confirmed period balances

Revision ID: 016_balance_cache
Revises: 015_closing_fiscal_period
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa

revision = "016_balance_cache"
down_revision = "015_closing_fiscal_period"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "balance_caches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("cumulative_debit", sa.Numeric(15, 0), nullable=False, server_default="0"),
        sa.Column("cumulative_credit", sa.Numeric(15, 0), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "user_id", "account_id", "year", "period",
            name="uq_balance_cache",
        ),
    )
    op.create_index("ix_balance_cache_user_year", "balance_caches", ["user_id", "year"])


def downgrade():
    op.drop_index("ix_balance_cache_user_year", table_name="balance_caches")
    op.drop_table("balance_caches")
