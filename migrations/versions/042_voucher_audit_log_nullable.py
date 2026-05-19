"""voucher_audit_logs テーブル復旧 + nullable / ondelete=SET NULL

退会時に Voucher は物理削除するが、`VoucherAuditLog`
(`action="deleted"` 含む) は電帳法スキャナ保存の 7 年保管義務に従い
**匿名化して保持** する。user_id / voucher_id を NULL 化できるよう
nullable + ondelete=SET NULL に変更する。

【歴史的バグへの対応】
本来は 026_voucher_audit_log で `voucher_audit_logs` テーブルが作成
されているはずだが、本番 DB の一部環境でテーブルが存在しないことを
確認した (alembic_version は 041 まで進んでいるが実テーブルがない)。
本マイグレーションでは:
- テーブルが存在しない環境: 新規作成 (nullable + ondelete=SET NULL の最終形)
- テーブルが存在する環境: ALTER COLUMN で nullable 化 + FK 再生成

これにより本番の状態に関わらず期待通り収束する。

Revision ID: 042_voucher_audit_log_nullable
Revises: 041_voucher_deleted_at
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "042_voucher_audit_log_nullable"
down_revision = "041_voucher_deleted_at"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    has_table = "voucher_audit_logs" in inspector.get_table_names()

    if not has_table:
        # 026 で作成されるはずが本番に存在しない環境向け復旧パス。
        # 最終形 (nullable + ondelete=SET NULL) で新規作成する。
        op.create_table(
            "voucher_audit_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "voucher_id", sa.Integer(),
                sa.ForeignKey("vouchers.id", ondelete="SET NULL"),
                nullable=True, index=True,
            ),
            sa.Column(
                "user_id", sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("action", sa.String(50), nullable=False),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(),
                nullable=False, server_default=sa.func.now(),
            ),
        )
        return

    # 既存テーブルを ALTER (026 で作成済の環境向けパス)
    # voucher_id を nullable + ondelete=SET NULL
    op.alter_column(
        "voucher_audit_logs", "voucher_id",
        existing_type=sa.Integer(), nullable=True,
    )
    op.drop_constraint(
        "voucher_audit_logs_voucher_id_fkey",
        "voucher_audit_logs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "voucher_audit_logs_voucher_id_fkey",
        "voucher_audit_logs", "vouchers",
        ["voucher_id"], ["id"],
        ondelete="SET NULL",
    )

    # user_id を nullable + ondelete=SET NULL
    op.alter_column(
        "voucher_audit_logs", "user_id",
        existing_type=sa.Integer(), nullable=True,
    )
    op.drop_constraint(
        "voucher_audit_logs_user_id_fkey",
        "voucher_audit_logs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "voucher_audit_logs_user_id_fkey",
        "voucher_audit_logs", "users",
        ["user_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    """downgrade は元の制約 (NOT NULL + 暗黙 RESTRICT) には戻さない。

    本マイグレーションは「nullable + ondelete=SET NULL」が最終形であり、
    歴史的バグ復旧のため新規作成も含むため、downgrade は実質的に不可能。
    完全に元に戻すなら 041 → 026 の手動再構築が必要。
    """
    # No-op (元の状態は環境により異なるため復元不可)
    pass
