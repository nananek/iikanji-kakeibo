"""E6 #113 §15.3: ai_drafts.discord_webhook_url / discord_message_id を DROP

AI 下書きの Discord 通知 (下書き作成時に Discord へ投稿し、仕訳登録時に
完了マークへ編集する機能) は旧 auto_import フロー専用だった。auto_import は
migration 048 で廃止済で、以降これらのカラムを書き込む経路は存在しない
(完全な死コード)。E2EE 化 (v5.0) でも自家ホスト連携は廃止方針のため、
カラムと通知サービス (app/services/notify.py) ごと物理削除する。

両カラムとも nullable=True で、実データは入っていない (auto_import 廃止後に
作成された下書きは全て NULL)。downgrade はカラムを再追加する (元値は復元不能
だが NULL 許容なので空で足りる)。

Revision ID: 065_drop_draft_discord_cols
Revises: 064_drop_webhook_configs
Create Date: 2026-06-03

注: revision ID は alembic_version.version_num (varchar(32)) に収まるよう
32 文字以内にすること。旧称 065_drop_ai_draft_discord_columns は 33 文字で
StringDataRightTruncation になった。
"""

from alembic import op
import sqlalchemy as sa


revision = "065_drop_draft_discord_cols"
down_revision = "064_drop_webhook_configs"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ai_drafts") as batch_op:
        batch_op.drop_column("discord_webhook_url")
        batch_op.drop_column("discord_message_id")


def downgrade():
    # 元の 020_draft_discord_message.py と同形 (いずれも nullable=True)。
    with op.batch_alter_table("ai_drafts") as batch_op:
        batch_op.add_column(
            sa.Column("discord_webhook_url", sa.String(500), nullable=True)
        )
        batch_op.add_column(
            sa.Column("discord_message_id", sa.String(30), nullable=True)
        )
