"""E3 PR-A: 仕訳 / 仕訳明細 / 医療費に encrypted_blob + blob_iv カラム追加

Phase E3 (#110) のスキーマ準備フェーズ。E2 で確立された
「BLOB + IV 別カラム保管 + クライアント完結 AES-256-GCM 暗号化」パターンを
journal_entries / journal_entry_lines / medical_expenses に適用する第 1 段階。

本マイグレでは:
- 3 テーブルに `encrypted_blob` (LargeBinary) + `blob_iv` (LargeBinary 12B) を
  ADD COLUMN (NULL 許容、既存行は NULL のまま)
- `journal_entries.fiscal_year` (smallint) を新設 (NULL 許容、平文)
  → date 暗号化後の年度フィルタ代替 (`GET /api/v1/journals?year=...` 等)

旧平文カラム (date / description / amount 等) の DROP は Phase E7 (一斉移行)
で実施。本 PR はカラム追加のみで dual storage 期間を開く。

設計書 §12.1 / §12.9 参照。

Revision ID: 050_e3_encrypted_blob_columns
Revises: 049_drop_fernet_columns
Create Date: 2026-05-25
"""

from alembic import op
import sqlalchemy as sa


revision = "050_e3_encrypted_blob_columns"
down_revision = "049_drop_fernet_columns"
branch_labels = None
depends_on = None


def upgrade():
    # journal_entries: blob + iv + fiscal_year
    with op.batch_alter_table("journal_entries") as batch_op:
        batch_op.add_column(
            sa.Column("encrypted_blob", sa.LargeBinary(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("blob_iv", sa.LargeBinary(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("fiscal_year", sa.SmallInteger(), nullable=True),
        )

    # journal_entry_lines: blob + iv
    with op.batch_alter_table("journal_entry_lines") as batch_op:
        batch_op.add_column(
            sa.Column("encrypted_blob", sa.LargeBinary(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("blob_iv", sa.LargeBinary(), nullable=True),
        )

    # medical_expenses: blob + iv
    with op.batch_alter_table("medical_expenses") as batch_op:
        batch_op.add_column(
            sa.Column("encrypted_blob", sa.LargeBinary(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("blob_iv", sa.LargeBinary(), nullable=True),
        )


def downgrade():
    with op.batch_alter_table("medical_expenses") as batch_op:
        batch_op.drop_column("blob_iv")
        batch_op.drop_column("encrypted_blob")

    with op.batch_alter_table("journal_entry_lines") as batch_op:
        batch_op.drop_column("blob_iv")
        batch_op.drop_column("encrypted_blob")

    with op.batch_alter_table("journal_entries") as batch_op:
        batch_op.drop_column("fiscal_year")
        batch_op.drop_column("blob_iv")
        batch_op.drop_column("encrypted_blob")
