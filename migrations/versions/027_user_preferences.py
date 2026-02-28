"""ユーザー表示設定カラムを追加

Revision ID: 027_user_preferences
Revises: 026_voucher_audit_log
"""
from alembic import op
import sqlalchemy as sa

revision = "027_user_preferences"
down_revision = "026_voucher_audit_log"


def upgrade():
    op.add_column("users", sa.Column("preferences", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("users", "preferences")
