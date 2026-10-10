"""AI設定に api_style と custom_headers カラムを追加

OpenAI 互換 API 向けに、エンドポイント種別 (chat_completions / responses) の
選択肢 (api_style) と、カスタム HTTP ヘッダー (custom_headers) を追加する。
既存行には server_default で安全な既定値が入る (非破壊)。

Revision ID: 049_ai_config_api_style_headers
Revises: 048_user_ai_base_url
Create Date: 2026-10-10
"""

from alembic import op
import sqlalchemy as sa


revision = "049_ai_config_api_style_headers"
down_revision = "048_user_ai_base_url"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_ai_configs",
        sa.Column(
            "api_style",
            sa.String(30),
            nullable=False,
            server_default="chat_completions",
        ),
    )
    op.add_column(
        "user_ai_configs",
        sa.Column(
            "custom_headers",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade():
    op.drop_column("user_ai_configs", "custom_headers")
    op.drop_column("user_ai_configs", "api_style")
