"""E2 PR-E-a: auto_import 機能廃止に伴い processed_files / auto_import_sources を削除

サーバ側 LLM 呼出経路の全廃 (E2-C シリーズ) + WebDAV credentials Fernet
廃止のため、auto_import 機能を丸ごと廃止する。

依存:
- processed_files.source_id → auto_import_sources.id (FK CASCADE) → 先に drop
- processed_files.draft_id → ai_drafts.id (FK SET NULL) → そのまま drop

WebhookConfig (webhook_configs テーブル) は account_deletion + 将来の
Discord 通知設定再構築用に維持する。

Revision ID: 048_drop_auto_import_tables
Revises: 047_ai_config_e2ee_columns
Create Date: 2026-05-24
"""

import sqlalchemy as sa
from alembic import op


revision = "048_drop_auto_import_tables"
down_revision = "047_ai_config_e2ee_columns"
branch_labels = None
depends_on = None


def upgrade():
    # FK 順序: processed_files → auto_import_sources の順で drop
    op.drop_table("processed_files")
    op.drop_table("auto_import_sources")


def downgrade():
    # 復元時は 017_auto_import.py のスキーマと同形
    op.create_table(
        "auto_import_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                   sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("credentials_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "processed_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id", sa.Integer(),
            sa.ForeignKey("auto_import_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_path", sa.String(500), nullable=False),
        sa.Column("etag", sa.String(100), nullable=True),
        sa.Column(
            "draft_id", sa.Integer(),
            sa.ForeignKey("ai_drafts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False, default="success"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "file_path", name="uq_source_file"),
    )
