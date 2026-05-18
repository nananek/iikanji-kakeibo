"""外部 AI API 呼び出し履歴テーブル ai_usage_logs を新設

ユーザーが自分のプロバイダーコンソール (OpenAI / Anthropic / Google) と
突合できるよう、サーバー側からの呼び出し記録を保存する。
プライバシー: プロンプト本文・レスポンス本文・API キーは保存しない。
トークン数とメタデータのみ。

Revision ID: 036_ai_usage_log
Revises: 035_drop_user_ai_base_url
Create Date: 2026-05-18
"""

from alembic import op
import sqlalchemy as sa


revision = "036_ai_usage_log"
down_revision = "035_drop_user_ai_base_url"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_usage_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("feature", sa.String(length=40), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "status", sa.String(length=20),
            nullable=False, server_default="ok",
        ),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_ai_usage_logs_user_id", "ai_usage_logs", ["user_id"]
    )
    op.create_index(
        "ix_ai_usage_logs_created_at", "ai_usage_logs", ["created_at"]
    )
    op.create_index(
        "ix_ai_usage_logs_user_created",
        "ai_usage_logs", ["user_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_ai_usage_logs_user_created", table_name="ai_usage_logs")
    op.drop_index("ix_ai_usage_logs_created_at", table_name="ai_usage_logs")
    op.drop_index("ix_ai_usage_logs_user_id", table_name="ai_usage_logs")
    op.drop_table("ai_usage_logs")
