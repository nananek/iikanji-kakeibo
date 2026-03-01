"""Account PK を (user_id, code) 複合キーに変更

Revision ID: 028_account_composite_pk
Revises: 027_user_preferences
"""
from alembic import op
import sqlalchemy as sa

revision = "028_account_composite_pk"
down_revision = "027_user_preferences"
branch_labels = None
depends_on = None


def upgrade():
    # --- journal_entry_lines ---
    op.add_column("journal_entry_lines",
                  sa.Column("account_user_id", sa.Integer(), nullable=True))
    op.add_column("journal_entry_lines",
                  sa.Column("account_code", sa.String(10), nullable=True))

    op.execute("""
        UPDATE journal_entry_lines
        SET account_user_id = a.user_id, account_code = a.code
        FROM accounts a
        WHERE journal_entry_lines.account_id = a.id
    """)

    op.drop_constraint("journal_entry_lines_account_id_fkey", "journal_entry_lines", type_="foreignkey")
    op.drop_column("journal_entry_lines", "account_id")

    op.alter_column("journal_entry_lines", "account_user_id", nullable=False)
    op.alter_column("journal_entry_lines", "account_code", nullable=False)

    # --- audit_grant_accounts ---
    op.add_column("audit_grant_accounts",
                  sa.Column("account_user_id", sa.Integer(), nullable=True))
    op.add_column("audit_grant_accounts",
                  sa.Column("account_code", sa.String(10), nullable=True))

    op.execute("""
        UPDATE audit_grant_accounts
        SET account_user_id = a.user_id, account_code = a.code
        FROM accounts a
        WHERE audit_grant_accounts.account_id = a.id
    """)

    op.drop_constraint("uq_audit_grant_account", "audit_grant_accounts", type_="unique")
    op.drop_constraint("audit_grant_accounts_account_id_fkey", "audit_grant_accounts", type_="foreignkey")
    op.drop_column("audit_grant_accounts", "account_id")

    op.alter_column("audit_grant_accounts", "account_user_id", nullable=False)
    op.alter_column("audit_grant_accounts", "account_code", nullable=False)

    # --- balance_caches ---
    op.add_column("balance_caches",
                  sa.Column("account_code", sa.String(10), nullable=True))

    op.execute("""
        UPDATE balance_caches
        SET account_code = a.code
        FROM accounts a
        WHERE balance_caches.account_id = a.id
    """)

    op.drop_constraint("uq_balance_cache", "balance_caches", type_="unique")
    op.drop_index("ix_balance_cache_user_year", "balance_caches")
    op.drop_constraint("balance_caches_account_id_fkey", "balance_caches", type_="foreignkey")
    op.drop_column("balance_caches", "account_id")

    op.alter_column("balance_caches", "account_code", nullable=False)

    # --- accounts: PK 変更 ---
    op.drop_constraint("uq_user_account_code", "accounts", type_="unique")
    op.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_pkey CASCADE")
    op.drop_column("accounts", "id")
    op.create_primary_key("accounts_pkey", "accounts", ["user_id", "code"])

    # --- FK 制約を追加 ---
    op.create_foreign_key(
        "fk_jel_account", "journal_entry_lines", "accounts",
        ["account_user_id", "account_code"], ["user_id", "code"],
    )
    op.create_foreign_key(
        "fk_aga_account", "audit_grant_accounts", "accounts",
        ["account_user_id", "account_code"], ["user_id", "code"],
    )
    op.create_foreign_key(
        "fk_bc_account", "balance_caches", "accounts",
        ["user_id", "account_code"], ["user_id", "code"],
    )

    # --- ユニーク制約・インデックスを再作成 ---
    op.create_unique_constraint(
        "uq_audit_grant_account", "audit_grant_accounts",
        ["audit_grant_id", "account_code"],
    )
    op.create_unique_constraint(
        "uq_balance_cache", "balance_caches",
        ["user_id", "account_code", "year", "period"],
    )
    op.create_index("ix_balance_cache_user_year", "balance_caches", ["user_id", "year"])


def downgrade():
    # 複合 PK から単一 id PK への巻き戻しは複雑すぎるためサポートしない
    raise NotImplementedError("Downgrade not supported for composite PK migration")
