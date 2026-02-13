"""Initial migration - create all tables

Revision ID: 001_initial
Revises:
Create Date: 2026-02-13
"""
from alembic import op
import sqlalchemy as sa

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # users
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(80), unique=True, nullable=False),
        sa.Column("email", sa.String(120), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # account_types
    op.create_table(
        "account_types",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("code", sa.String(10), unique=True, nullable=False),
        sa.Column("normal_balance", sa.String(10), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
    )

    # accounts
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_type_id", sa.Integer(), sa.ForeignKey("account_types.id"), nullable=False),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(255), server_default=""),
        sa.Column("tax_category", sa.String(50), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "code", name="uq_user_account_code"),
    )

    # journal_entries
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("entry_number", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("source", sa.String(20), nullable=False, server_default="journal"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "entry_number", name="uq_user_entry_number"),
    )

    # journal_entry_lines
    op.create_table(
        "journal_entry_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("journal_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("debit_amount", sa.Numeric(12, 0), nullable=False, server_default="0"),
        sa.Column("credit_amount", sa.Numeric(12, 0), nullable=False, server_default="0"),
        sa.Column("description", sa.String(255), server_default=""),
    )

    # medical_expenses
    op.create_table(
        "medical_expenses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("patient_name", sa.String(100), nullable=False),
        sa.Column("hospital_name", sa.String(200), nullable=False),
        sa.Column("treatment_description", sa.String(255), nullable=False, server_default=""),
        sa.Column("amount_paid", sa.Integer(), nullable=False),
        sa.Column("insurance_reimbursement", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("medical_expenses")
    op.drop_table("journal_entry_lines")
    op.drop_table("journal_entries")
    op.drop_table("accounts")
    op.drop_table("account_types")
    op.drop_table("users")
