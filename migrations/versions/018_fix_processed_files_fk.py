"""Fix processed_files.draft_id FK to SET NULL on delete

Revision ID: 018_fix_processed_files_fk
Revises: 017_auto_import
Create Date: 2026-02-19
"""

from alembic import op

revision = "018_fix_processed_files_fk"
down_revision = "017_auto_import"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("processed_files_draft_id_fkey", "processed_files", type_="foreignkey")
    op.create_foreign_key(
        "processed_files_draft_id_fkey",
        "processed_files",
        "ai_drafts",
        ["draft_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint("processed_files_draft_id_fkey", "processed_files", type_="foreignkey")
    op.create_foreign_key(
        "processed_files_draft_id_fkey",
        "processed_files",
        "ai_drafts",
        ["draft_id"],
        ["id"],
    )
