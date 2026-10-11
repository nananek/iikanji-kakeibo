"""AI設定に max_turns (エージェント最大ターン数) カラムを追加

AI証憑仕訳のエージェントループの最大ターン数をユーザーが設定できるようにする。
既存行には server_default で既定値 10 が入る (非破壊)。

Revision ID: 050_ai_config_max_turns
Revises: 049_ai_config_api_style_headers
Create Date: 2026-10-11
"""

from alembic import op
import sqlalchemy as sa


revision = "050_ai_config_max_turns"
down_revision = "049_ai_config_api_style_headers"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_ai_configs",
        sa.Column(
            "max_turns",
            sa.Integer(),
            nullable=False,
            server_default="10",
        ),
    )


def downgrade():
    op.drop_column("user_ai_configs", "max_turns")
