"""Drop auto_import tables (WebDAV 自動取込の撤去)

Revision ID: 051_drop_auto_import_tables
Revises: 050_ai_config_max_turns
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "051_drop_auto_import_tables"
down_revision = "050_ai_config_max_turns"
branch_labels = None
depends_on = None


def upgrade():
    # processed_files → auto_import_sources の順で drop (FK 依存順)。
    # webhook_configs は通知機能で継続利用するため残す。
    op.drop_table("processed_files")
    op.drop_table("auto_import_sources")


def downgrade():
    op.create_table(
        "auto_import_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("credentials_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "processed_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("auto_import_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_path", sa.String(500), nullable=False),
        sa.Column("etag", sa.String(100), nullable=True),
        sa.Column(
            "draft_id", sa.Integer(), sa.ForeignKey("ai_drafts.id"),
            nullable=True,
        ),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="success",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "processed_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("source_id", "file_path", name="uq_source_file"),
    )
