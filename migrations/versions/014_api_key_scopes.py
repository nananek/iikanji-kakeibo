"""Add scopes column to api_keys

Revision ID: 014_api_key_scopes
Revises: 013_api_keys
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa

revision = "014_api_key_scopes"
down_revision = "013_api_keys"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "api_keys",
        sa.Column(
            "scopes",
            sa.String(200),
            nullable=False,
            server_default="journals:create",
        ),
    )


def downgrade():
    op.drop_column("api_keys", "scopes")
