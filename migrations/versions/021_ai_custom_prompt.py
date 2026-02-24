"""Add custom_prompt to user_ai_configs

Revision ID: 021_ai_custom_prompt
Revises: 020_draft_discord_message
Create Date: 2026-02-24
"""

from alembic import op
import sqlalchemy as sa

revision = "021_ai_custom_prompt"
down_revision = "020_draft_discord_message"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_ai_configs",
        sa.Column("custom_prompt", sa.Text(), nullable=False, server_default=""),
    )


def downgrade():
    op.drop_column("user_ai_configs", "custom_prompt")
