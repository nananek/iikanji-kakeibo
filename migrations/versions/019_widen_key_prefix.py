"""Widen api_keys.key_prefix from varchar(12) to varchar(20)

Revision ID: 019_widen_key_prefix
Revises: 018_fix_processed_files_fk
Create Date: 2026-02-19
"""

from alembic import op
import sqlalchemy as sa

revision = "019_widen_key_prefix"
down_revision = "018_fix_processed_files_fk"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "api_keys",
        "key_prefix",
        type_=sa.String(20),
        existing_type=sa.String(12),
        existing_nullable=False,
    )


def downgrade():
    op.alter_column(
        "api_keys",
        "key_prefix",
        type_=sa.String(12),
        existing_type=sa.String(20),
        existing_nullable=False,
    )
