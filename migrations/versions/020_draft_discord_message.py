"""Add discord_webhook_url and discord_message_id to ai_drafts

Revision ID: 020_draft_discord_message
Revises: 019_widen_key_prefix
Create Date: 2026-02-24
"""

from alembic import op
import sqlalchemy as sa

revision = "020_draft_discord_message"
down_revision = "019_widen_key_prefix"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_drafts", sa.Column("discord_webhook_url", sa.String(500), nullable=True))
    op.add_column("ai_drafts", sa.Column("discord_message_id", sa.String(30), nullable=True))


def downgrade():
    op.drop_column("ai_drafts", "discord_message_id")
    op.drop_column("ai_drafts", "discord_webhook_url")
