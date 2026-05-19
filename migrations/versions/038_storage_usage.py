"""StorageUsage テーブル新設 (Phase 5 #70)

証憑画像のユーザー別使用バイト数を集計するシンプルなテーブル。
アップロード/削除時に加減算する。クオータ上限超過のリアルタイム判定
と、UI 上の残量表示に使う。

Revision ID: 038_storage_usage
Revises: 037_accepted_terms_version
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "038_storage_usage"
down_revision = "037_accepted_terms_version"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "storage_usage",
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id"), primary_key=True,
        ),
        sa.Column(
            "used_bytes", sa.BigInteger(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_table("storage_usage")
