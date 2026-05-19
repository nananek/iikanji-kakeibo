"""Voucher 論理削除化 (Phase 5 #70 / 電帳法証跡永続化)

`vouchers.deleted_at` カラムを追加し、Voucher の物理削除を論理削除に
変更する。これにより `voucher_audit_logs.voucher_id` の FK RESTRICT
制約下でも `action="deleted"` の AuditLog を永続化でき、電帳法スキャナ
保存の「訂正削除の事実と内容を確認できること」要件を完全に満たす。

`Voucher.active()` クラスメソッドで `deleted_at IS NULL` 絞込を一元化、
一覧・検索・REST API は active() スコープで透過的に動作する。

Revision ID: 041_voucher_deleted_at
Revises: 040_ai_draft_file_size
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "041_voucher_deleted_at"
down_revision = "040_ai_draft_file_size"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "vouchers",
        sa.Column(
            "deleted_at", sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_vouchers_deleted_at",
        "vouchers",
        ["deleted_at"],
    )


def downgrade():
    op.drop_index("ix_vouchers_deleted_at", table_name="vouchers")
    op.drop_column("vouchers", "deleted_at")
