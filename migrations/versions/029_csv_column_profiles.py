"""CSV列マッピングプロファイルテーブル

Revision ID: 029_csv_column_profiles
Revises: 028_account_composite_pk
"""
from alembic import op
import sqlalchemy as sa

revision = "029_csv_column_profiles"
down_revision = "028_account_composite_pk"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "csv_column_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_code", sa.String(10), nullable=False),
        sa.Column("date_col", sa.Integer(), nullable=False),
        sa.Column("desc_col", sa.Integer(), nullable=False),
        sa.Column("deposit_col", sa.Integer(), nullable=True),
        sa.Column("withdrawal_col", sa.Integer(), nullable=True),
        sa.Column("amount_col", sa.Integer(), nullable=True),
        sa.Column(
            "date_format", sa.String(30), nullable=False,
            server_default="%Y/%m/%d",
        ),
        sa.Column(
            "amount_mode", sa.String(10), nullable=False,
            server_default="separate",
        ),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_code"],
            ["accounts.user_id", "accounts.code"],
            name="fk_csv_profile_account",
        ),
        sa.UniqueConstraint(
            "user_id", "account_code",
            name="uq_csv_profile_user_account",
        ),
    )


def downgrade():
    op.drop_table("csv_column_profiles")
