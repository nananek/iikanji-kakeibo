"""Voucher.file_size カラムを追加 (Phase 5 #70)

容量計上のためのファイルサイズ (バイト) を Voucher 単位で保持する。
削除時の record_delete でも参照するため、新規 Voucher 作成時に
セットする。既存の Voucher は NULL のままで、整合性監査バッチ
(後続 PR) で値を埋める想定。

Revision ID: 039_voucher_file_size
Revises: 038_storage_usage
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "039_voucher_file_size"
down_revision = "038_storage_usage"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "vouchers",
        sa.Column("file_size", sa.BigInteger(), nullable=True),
    )


def downgrade():
    op.drop_column("vouchers", "file_size")
