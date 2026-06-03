"""E6 #113 §15.4 PR-2: export_jobs テーブル (サーバ一時保存エクスポート)

全データエクスポートのサーバ保存 + メール配信経路で使う。クライアントが
生成・パスフレーズ暗号化した zip (.ikexport) を storage に一時保存し、その
メタを export_jobs で管理する。サーバは暗号文を預かるだけで平文を持たない。

期限切れ (expires_at 経過) はダウンロード時 410 で弾き、物理削除は
`flask export-cleanup` (PR-3) が行う。

Revision ID: 066_export_jobs
Revises: 065_drop_draft_discord_cols
Create Date: 2026-06-03

注: revision ID は alembic_version.version_num (varchar(32)) に収まるよう
32 文字以内にすること。
"""

from alembic import op
import sqlalchemy as sa


revision = "066_export_jobs"
down_revision = "065_drop_draft_discord_cols"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("download_count", sa.SmallInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_export_jobs_user_expires",
        "export_jobs",
        ["user_id", "expires_at"],
    )


def downgrade():
    op.drop_index("ix_export_jobs_user_expires", table_name="export_jobs")
    op.drop_table("export_jobs")
