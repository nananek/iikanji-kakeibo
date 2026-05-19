"""vouchers の active 行向け partial index 追加 (Phase 5 後続)

`Voucher.active()` (= `deleted_at IS NULL`) 絞込が大多数のクエリで使われ
るため、PostgreSQL の partial index で active 行のみを索引化する。論理
削除済が大量蓄積した場合のクエリ性能を改善する。

既存の通常 index (`ix_vouchers_deleted_at`) は退役し、削除済も含めた
監査ログ参照などのために残す。

Revision ID: 045_voucher_active_partial_index
Revises: 044_invitation_tokens
Create Date: 2026-05-19
"""

from alembic import op


revision = "045_voucher_active_partial_index"
down_revision = "044_invitation_tokens"
branch_labels = None
depends_on = None


def upgrade():
    # PostgreSQL: deleted_at IS NULL の partial index
    # (SQLite はそもそも自動 FK 強制も partial index も非対応だが、本マイグ
    # レーションは PostgreSQL 本番運用前提)。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vouchers_active_user "
        "ON vouchers (user_id) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vouchers_active_journal "
        "ON vouchers (journal_entry_id) "
        "WHERE deleted_at IS NULL AND journal_entry_id IS NOT NULL"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_vouchers_active_user")
    op.execute("DROP INDEX IF EXISTS ix_vouchers_active_journal")
