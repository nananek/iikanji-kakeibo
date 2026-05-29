"""E3-F: journal_entries に is_closing / fiscal_month を追加 + backfill

dual-read 撤去の最終段 (#220) で旧平文カラム `source` / `fiscal_period` を
DROP するため、それらに依存していたサーバ側ロジックの代替となる平文カラムを
先行して追加する。

- `is_closing` (Boolean): 旧 `source == 'closing'` (損益振替の自動生成仕訳) の
  判定を引き継ぐ。`fiscal.generate_closing_entries` / `delete_closing_entries` /
  `check_entry_modifiable` で参照。
- `fiscal_month` (SmallInteger): 旧 `fiscal_period` と同じ値域
  (0=期首振戻, 1-12=通常月, 13-15=決算月, 16=損益振替)。`date` 暗号化後の
  月単位フィルタ・期間判定用。

backfill 後の DROP / NOT NULL 化は次マイグレ 055 で行う (中間状態でも
アプリが起動可能なよう 2 段階に分離)。

Revision ID: 054_e3f_add_closing_month
Revises: 053_drop_balance_caches
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa


revision = "054_e3f_add_closing_month"
down_revision = "053_drop_balance_caches"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("journal_entries") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_closing",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        batch_op.add_column(
            sa.Column("fiscal_month", sa.SmallInteger(), nullable=True),
        )

    # backfill: 旧平文カラムから新カラムへ値を移す。
    op.execute(
        "UPDATE journal_entries SET is_closing = TRUE WHERE source = 'closing'"
    )
    # fiscal_year が未設定の旧行は date から補完 (period フィルタの前提)。
    op.execute(
        "UPDATE journal_entries SET fiscal_year = EXTRACT(YEAR FROM date)"
        " WHERE fiscal_year IS NULL AND date IS NOT NULL"
    )
    # fiscal_period が明示されていた行はそのまま、NULL だった行
    # (= 日付の月で暗黙判定していた通常仕訳) は date.month から補完する。
    op.execute(
        "UPDATE journal_entries SET fiscal_month = fiscal_period"
        " WHERE fiscal_period IS NOT NULL"
    )
    op.execute(
        "UPDATE journal_entries SET fiscal_month = EXTRACT(MONTH FROM date)"
        " WHERE fiscal_month IS NULL AND date IS NOT NULL"
    )

    # 月単位フィルタ用の複合インデックス (fiscal.period_condition 等)。
    op.create_index(
        "ix_journal_entries_user_fiscal_year_month",
        "journal_entries",
        ["user_id", "fiscal_year", "fiscal_month"],
    )
    # closing 仕訳の撤回 (delete_closing_entries) 用の partial index。
    op.create_index(
        "ix_journal_entries_user_is_closing",
        "journal_entries",
        ["user_id"],
        postgresql_where=sa.text("is_closing"),
    )

    # 旧平文カラムのうち DB default を持たない NOT NULL 列を nullable に緩和する。
    # PR-D-1 のコードはこれら平文列への書き込みを停止するため、055 で DROP する
    # までの中間状態で新規行が NULL を入れても INSERT が失敗しないようにする
    # (date / amount_paid は ORM default を持たないため緩和必須)。
    op.alter_column("journal_entries", "date", nullable=True)
    op.alter_column("medical_expenses", "date", nullable=True)
    op.alter_column("medical_expenses", "amount_paid", nullable=True)


def downgrade():
    op.alter_column("medical_expenses", "amount_paid", nullable=False)
    op.alter_column("medical_expenses", "date", nullable=False)
    op.alter_column("journal_entries", "date", nullable=False)
    op.drop_index(
        "ix_journal_entries_user_is_closing",
        table_name="journal_entries",
    )
    op.drop_index(
        "ix_journal_entries_user_fiscal_year_month",
        table_name="journal_entries",
    )
    with op.batch_alter_table("journal_entries") as batch_op:
        batch_op.drop_column("fiscal_month")
        batch_op.drop_column("is_closing")
