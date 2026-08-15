"""OpenAI 互換 API 用に user_ai_configs.base_url を再追加

Ollama 時代 (022) に存在した base_url カラム (String(500)) を復活させる。
ユーザーが OpenAI 互換 API (OpenCode Go 等) のエンドポイントを自由に設定
できるようにする。既存行には server_default で空文字が入る (非破壊)。

Revision ID: 048_user_ai_base_url
Revises: 047_drop_passkey_recovery
Create Date: 2026-08-15
"""

from alembic import op
import sqlalchemy as sa


revision = "048_user_ai_base_url"
down_revision = "047_drop_passkey_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_ai_configs",
        sa.Column("base_url", sa.String(500), nullable=False, server_default=""),
    )


def downgrade():
    op.drop_column("user_ai_configs", "base_url")
