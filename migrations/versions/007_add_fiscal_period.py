"""Add fiscal_period to journal_entries and fiscal_closes table

Revision ID: 007_fiscal_period
Revises: 006_medical_cascade
Create Date: 2026-02-14
"""
from alembic import op
import sqlalchemy as sa

revision = "007_fiscal_period"
down_revision = "006_medical_cascade"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "journal_entries",
        sa.Column("fiscal_period", sa.Integer(), nullable=True),
    )

    op.create_table(
        "fiscal_closes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("closed_period", sa.Integer(), nullable=False, server_default="-1"),
        sa.UniqueConstraint("user_id", "year", name="uq_fiscal_close_user_year"),
    )


def downgrade():
    op.drop_table("fiscal_closes")
    op.drop_column("journal_entries", "fiscal_period")
