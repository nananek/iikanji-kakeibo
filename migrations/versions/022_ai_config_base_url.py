"""Add base_url to user_ai_configs for Ollama support

Revision ID: 022_ai_config_base_url
Revises: 021_ai_custom_prompt
Create Date: 2026-02-24
"""

from alembic import op
import sqlalchemy as sa

revision = "022_ai_config_base_url"
down_revision = "021_ai_custom_prompt"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_ai_configs",
        sa.Column("base_url", sa.String(500), nullable=False, server_default=""),
    )


def downgrade():
    op.drop_column("user_ai_configs", "base_url")
