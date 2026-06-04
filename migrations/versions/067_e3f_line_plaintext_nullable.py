"""E3-F (#338 item5/Phase 5a): journal_entry_lines の平文列を nullable 化

#338 方針B の write 停止 (item5) → DROP (item8) に向けた準備。仕訳明細の平文メタ
account_code / debit_amount / credit_amount は read 側を全て client 化済み
(item4 応答平文除去 + Phase R レポート集計 client 化)。次に write を停止するが、
account_code は NOT NULL (default 無し) のため、write を止めると INSERT が落ちる。
本マイグレで 3 列を nullable に緩和し、後続 PR で create_journal_entry 等が NULL を
書けるようにする。

複合 FK fk_jel_account (account_user_id, account_code) → accounts(user_id, code) は
維持する。account_code が NULL の行は MATCH SIMPLE により FK 検査の対象外になる
(NULL 部分を含む複合 FK は強制されない) ため、nullable 化と FK の共存に問題はない。

DROP (account_code/debit_amount/credit_amount + FK 撤去) は item8 (068) で行う。

Revision ID: 067_e3f_line_plaintext_nullable
Revises: 066_export_jobs
Create Date: 2026-06-04
"""

from alembic import op
import sqlalchemy as sa


revision = "067_e3f_line_plaintext_nullable"
down_revision = "066_export_jobs"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("journal_entry_lines") as b:
        b.alter_column(
            "account_code", existing_type=sa.String(10), nullable=True
        )
        b.alter_column(
            "debit_amount", existing_type=sa.Numeric(12, 0), nullable=True
        )
        b.alter_column(
            "credit_amount", existing_type=sa.Numeric(12, 0), nullable=True
        )


def downgrade():
    # #338 Phase 5 (write 停止) 後は account_code が NULL の行が存在しうる。これを
    # NOT NULL へ戻すために空文字 '' を埋めるが、'' は複合 FK fk_jel_account
    # (account_user_id, account_code) → accounts(user_id, code) を満たさないため、
    # 先に FK を drop してから fill + NOT NULL 復元する (FK 違反で downgrade が
    # 落ちるのを防ぐ — #351 review 指摘)。downgrade は write 停止前の状態へ完全に
    # 戻せない (NULL 行に妥当な account_code が存在しない) ため best-effort で、FK は
    # 再作成しない。item8 (068) で 3 列ごと物理 DROP され FK も恒久撤去される。
    with op.batch_alter_table("journal_entry_lines") as b:
        b.drop_constraint("fk_jel_account", type_="foreignkey")
    op.execute(
        sa.text(
            "UPDATE journal_entry_lines SET account_code = '' "
            "WHERE account_code IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE journal_entry_lines SET debit_amount = 0 "
            "WHERE debit_amount IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE journal_entry_lines SET credit_amount = 0 "
            "WHERE credit_amount IS NULL"
        )
    )
    with op.batch_alter_table("journal_entry_lines") as b:
        b.alter_column(
            "account_code", existing_type=sa.String(10), nullable=False
        )
        b.alter_column(
            "debit_amount", existing_type=sa.Numeric(12, 0), nullable=False
        )
        b.alter_column(
            "credit_amount", existing_type=sa.Numeric(12, 0), nullable=False
        )
