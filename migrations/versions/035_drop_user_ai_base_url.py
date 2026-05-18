"""Ollama サポート終了に伴い user_ai_configs.base_url カラムを削除

llama.cpp はサーバー管理者が用意する任意機能となり、エンドポイントは
環境変数 LLAMA_CPP_URL でアプリ全体に注入される。ユーザー個別の base_url
は保持しない方針。

Revision ID: 035_drop_user_ai_base_url
Revises: 034_passkey_only
Create Date: 2026-05-18
"""

from alembic import op
import sqlalchemy as sa


revision = "035_drop_user_ai_base_url"
down_revision = "034_passkey_only"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_ai_configs") as batch_op:
        batch_op.drop_column("base_url")


def downgrade():
    with op.batch_alter_table("user_ai_configs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "base_url", sa.String(length=500),
                nullable=False, server_default="",
            )
        )
