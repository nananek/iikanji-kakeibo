"""Add batch_id to journal_entries

Revision ID: 005_add_batch_id
Revises: 004_add_cost_type
Create Date: 2026-02-14
"""
from alembic import op
import sqlalchemy as sa

revision = "005_add_batch_id"
down_revision = "004_add_cost_type"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "journal_entries",
        sa.Column("batch_id", sa.String(36), nullable=True),
    )
    op.create_index(
        "ix_journal_entries_batch_id",
        "journal_entries",
        ["batch_id"],
    )


def downgrade():
    op.drop_index("ix_journal_entries_batch_id", table_name="journal_entries")
    op.drop_column("journal_entries", "batch_id")
