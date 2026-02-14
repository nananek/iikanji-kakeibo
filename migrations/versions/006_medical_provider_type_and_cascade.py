"""Add provider_type to medical_expenses and cascade delete on journal_entry_id

Revision ID: 006_medical_cascade
Revises: 005_add_batch_id
Create Date: 2026-02-14
"""
from alembic import op
import sqlalchemy as sa

revision = "006_medical_cascade"
down_revision = "005_add_batch_id"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "medical_expenses",
        sa.Column("provider_type", sa.String(20), nullable=True),
    )

    # patient_name, hospital_name を NOT NULL DEFAULT '' に変更
    op.alter_column(
        "medical_expenses", "patient_name",
        server_default="",
        existing_type=sa.String(100),
        existing_nullable=False,
    )
    op.alter_column(
        "medical_expenses", "hospital_name",
        server_default="",
        existing_type=sa.String(200),
        existing_nullable=False,
    )

    # journal_entry_id の外部キーを ON DELETE CASCADE に変更
    op.drop_constraint(
        "medical_expenses_journal_entry_id_fkey",
        "medical_expenses",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "medical_expenses_journal_entry_id_fkey",
        "medical_expenses",
        "journal_entries",
        ["journal_entry_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade():
    op.drop_constraint(
        "medical_expenses_journal_entry_id_fkey",
        "medical_expenses",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "medical_expenses_journal_entry_id_fkey",
        "medical_expenses",
        "journal_entries",
        ["journal_entry_id"],
        ["id"],
    )

    op.alter_column(
        "medical_expenses", "hospital_name",
        server_default=None,
        existing_type=sa.String(200),
        existing_nullable=False,
    )
    op.alter_column(
        "medical_expenses", "patient_name",
        server_default=None,
        existing_type=sa.String(100),
        existing_nullable=False,
    )

    op.drop_column("medical_expenses", "provider_type")
