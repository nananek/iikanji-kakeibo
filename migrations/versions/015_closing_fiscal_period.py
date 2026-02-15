"""Move closing entries to fiscal_period 16

Revision ID: 015_closing_fiscal_period
Revises: 014_api_key_scopes
Create Date: 2026-02-15
"""

from alembic import op

revision = "015_closing_fiscal_period"
down_revision = "014_api_key_scopes"
branch_labels = None
depends_on = None


def upgrade():
    # source='closing' の仕訳は全て fiscal_period=16 に移行
    # (以前は fiscal_period=15 で生成されていた)
    op.execute(
        "UPDATE journal_entries SET fiscal_period = 16"
        " WHERE source = 'closing'"
    )


def downgrade():
    op.execute(
        "UPDATE journal_entries SET fiscal_period = 15"
        " WHERE source = 'closing' AND fiscal_period = 16"
    )
