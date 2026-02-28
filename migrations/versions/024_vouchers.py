"""Create vouchers table and add file_hash to ai_drafts

Revision ID: 024_vouchers
Revises: 023_externalize_image_storage
Create Date: 2026-02-28
"""

from alembic import op
import sqlalchemy as sa

revision = "024_vouchers"
down_revision = "023_externalize_image_storage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "vouchers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "journal_entry_id",
            sa.Integer(),
            sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("image_key", sa.String(255), nullable=False),
        sa.Column("image_mime", sa.String(50), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=True),
        sa.Column("file_hash", sa.String(64), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vouchers_user_id", "vouchers", ["user_id"])
    op.create_index("ix_vouchers_journal_entry_id", "vouchers", ["journal_entry_id"])

    op.add_column("ai_drafts", sa.Column("file_hash", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("ai_drafts", "file_hash")
    op.drop_index("ix_vouchers_journal_entry_id", table_name="vouchers")
    op.drop_index("ix_vouchers_user_id", table_name="vouchers")
    op.drop_table("vouchers")
