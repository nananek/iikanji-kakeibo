"""OAuth デバイス・トークンに read_only フラグを追加

Revision ID: 033_oauth_readonly
Revises: 032_oauth_device_flow
Create Date: 2026-05-02
"""

from alembic import op
import sqlalchemy as sa


revision = "033_oauth_readonly"
down_revision = "032_oauth_device_flow"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "oauth_devices",
        sa.Column(
            "read_only", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "oauth_tokens",
        sa.Column(
            "read_only", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade():
    op.drop_column("oauth_tokens", "read_only")
    op.drop_column("oauth_devices", "read_only")
