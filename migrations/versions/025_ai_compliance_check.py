"""user_ai_configs に compliance_check カラムを追加

Revision ID: 025_ai_compliance_check
Revises: 024_vouchers
"""
from alembic import op
import sqlalchemy as sa

revision = "025_ai_compliance_check"
down_revision = "024_vouchers"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_ai_configs") as batch_op:
        batch_op.add_column(
            sa.Column("compliance_check", sa.Boolean(), nullable=False, server_default="0")
        )


def downgrade():
    with op.batch_alter_table("user_ai_configs") as batch_op:
        batch_op.drop_column("compliance_check")
