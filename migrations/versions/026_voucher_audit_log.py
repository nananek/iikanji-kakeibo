"""証憑操作ログテーブルを追加

Revision ID: 026_voucher_audit_log
Revises: 025_ai_compliance_check
"""
from alembic import op
import sqlalchemy as sa

revision = "026_voucher_audit_log"
down_revision = "025_ai_compliance_check"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "voucher_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("voucher_id", sa.Integer(), sa.ForeignKey("vouchers.id"), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("voucher_audit_logs")
