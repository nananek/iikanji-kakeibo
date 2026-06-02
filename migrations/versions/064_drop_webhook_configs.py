"""E6 #113 §15.3: webhook_configs テーブルを廃止する

Webhook 通知設定 (WebhookConfig) は v4.x で「将来の Discord 通知設定 UI 再構築
用」に温存されていたが、実際に通知を送る経路からは参照されない休眠テーブルで
あり、E2EE 化 (v5.0) 後も利用予定がない。死コード一掃 (Phase E6) として物理
削除する。

backup/export からは webhook_configs を除外済 (api.py)。restore は未知キーを
無視するため、webhook_configs を含む旧 backup も後方互換で読める (該当データは
復元されない)。

依存:
- webhook_configs.user_id → users.id (FK)。他テーブルからの被参照はなし。

Revision ID: 064_drop_webhook_configs
Revises: 063_drop_audit_grant_status
Create Date: 2026-06-03
"""

import sqlalchemy as sa
from alembic import op


revision = "064_drop_webhook_configs"
down_revision = "063_drop_audit_grant_status"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("webhook_configs")


def downgrade():
    # 復元時は 017_auto_import.py の webhook_configs スキーマと同形
    op.create_table(
        "webhook_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("webhook_url", sa.String(500), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column(
            "events_json",
            sa.Text(),
            nullable=False,
            server_default='["import_success"]',
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
