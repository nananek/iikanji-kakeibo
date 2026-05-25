"""E3 PR-B: journal_entries.fiscal_year に複合インデックスを追加

E3 PR-A (#181) のレビュー Medium 指摘の解消。
`fiscal_year` は date 暗号化後の年度フィルタ用カラム
(`GET /api/v1/journals?year=...`) として導入されたが、インデックスなしでは
仕訳件数増加時にフルスキャンになる。

`(user_id, fiscal_year)` の複合インデックスで、ユーザー単位の年度別取得を
高速化する。

Revision ID: 051_e3_fiscal_year_index
Revises: 050_e3_encrypted_blob_columns
Create Date: 2026-05-25
"""

from alembic import op


revision = "051_e3_fiscal_year_index"
down_revision = "050_e3_encrypted_blob_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_journal_entries_user_fiscal_year",
        "journal_entries",
        ["user_id", "fiscal_year"],
    )


def downgrade():
    op.drop_index(
        "ix_journal_entries_user_fiscal_year",
        table_name="journal_entries",
    )
