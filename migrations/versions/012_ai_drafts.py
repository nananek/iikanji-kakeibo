"""Add ai_drafts table

Revision ID: 012_ai_drafts
Revises: 011_capital_system_role
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa

revision = "012_ai_drafts"
down_revision = "011_capital_system_role"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("image_data", sa.LargeBinary(), nullable=False),
        sa.Column("image_mime", sa.String(50), nullable=False),
        sa.Column("comment", sa.String(500), server_default=""),
        sa.Column("suggestions_json", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="analyzed"
        ),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade():
    op.drop_table("ai_drafts")
