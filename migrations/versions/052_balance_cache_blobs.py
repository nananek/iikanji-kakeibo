"""E3 PR-E-1: balance_cache_blobs テーブル新設

確定済み期間の残高キャッシュをクライアント暗号化済み blob で保存する
新テーブルを追加する。既存の `balance_caches` (平文) はそのまま残し、
dual storage で運用 (E3-F で平文側を撤去予定)。

スキーマ:
  - PK: id
  - UNIQUE (user_id, year, period)   ← 1 (user, year, period) で 1 行
  - encrypted_blob LargeBinary       ← クライアント側 AES-GCM 暗号文 (JSON)
  - blob_iv LargeBinary(12)          ← AES-GCM IV (12 byte)
  - updated_at                       ← 最終更新時刻

AAD は `b"bcb\\0" + uint64_be(user_id) + b"\\0" + uint64_be(year*100+period)`
(クライアント側で構築)。

Revision ID: 052_balance_cache_blobs
Revises: 051_e3_fiscal_year_index
Create Date: 2026-05-26
"""

from alembic import op
import sqlalchemy as sa


revision = "052_balance_cache_blobs"
down_revision = "051_e3_fiscal_year_index"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "balance_cache_blobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("encrypted_blob", sa.LargeBinary(), nullable=False),
        sa.Column("blob_iv", sa.LargeBinary(12), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_balance_cache_blobs_user",
        ),
        sa.UniqueConstraint(
            "user_id", "year", "period",
            name="uq_balance_cache_blobs",
        ),
        sa.Index(
            "ix_balance_cache_blobs_user_year",
            "user_id", "year",
        ),
    )


def downgrade():
    op.drop_table("balance_cache_blobs")
