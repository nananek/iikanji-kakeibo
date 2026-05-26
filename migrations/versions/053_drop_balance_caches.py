"""E3-F-6: balance_caches テーブル drop

BCB (balance_cache_blobs) への完全移行が完了したので旧 balance_caches を撤去する。

- 028_account_composite_pk で account_code カラム + 複合 FK + uq_balance_cache に
  リスキーミングされた状態を起点に downgrade で復元する。
- BCB 側 (balance_cache_blobs, 052) は変更しない。

Revision ID: 053_drop_balance_caches
Revises: 052_balance_cache_blobs
Create Date: 2026-05-27
"""

from alembic import op
import sqlalchemy as sa


revision = "053_drop_balance_caches"
down_revision = "052_balance_cache_blobs"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "balance_caches" not in inspector.get_table_names():
        return
    indexes = {idx["name"] for idx in inspector.get_indexes("balance_caches")}
    if "ix_balance_cache_user_year" in indexes:
        op.drop_index(
            "ix_balance_cache_user_year", table_name="balance_caches",
        )
    op.drop_table("balance_caches")


def downgrade():
    op.create_table(
        "balance_caches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_code", sa.String(length=10), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column(
            "cumulative_debit", sa.Numeric(15, 0),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "cumulative_credit", sa.Numeric(15, 0),
            nullable=False, server_default="0",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="balance_caches_user_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_code"],
            ["accounts.user_id", "accounts.code"],
            name="fk_bc_account",
        ),
        sa.UniqueConstraint(
            "user_id", "account_code", "year", "period",
            name="uq_balance_cache",
        ),
    )
    op.create_index(
        "ix_balance_cache_user_year", "balance_caches", ["user_id", "year"],
    )
