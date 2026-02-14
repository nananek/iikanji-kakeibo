"""Add audit account support (user_type, audit_grants, audit_grant_accounts, 事業主科目)

Revision ID: 008_audit_accounts
Revises: 007_fiscal_period
Create Date: 2026-02-14
"""
from alembic import op
import sqlalchemy as sa

revision = "008_audit_accounts"
down_revision = "007_fiscal_period"
branch_labels = None
depends_on = None


def upgrade():
    # User に user_type 追加
    op.add_column(
        "users",
        sa.Column(
            "user_type",
            sa.String(10),
            nullable=False,
            server_default="personal",
        ),
    )

    # audit_grants テーブル
    op.create_table(
        "audit_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("auditor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("permission_level", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="draft"),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_user_id", "auditor_user_id", name="uq_audit_grant_owner_auditor"),
    )

    # audit_grant_accounts テーブル
    op.create_table(
        "audit_grant_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "audit_grant_id",
            sa.Integer(),
            sa.ForeignKey("audit_grants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.UniqueConstraint("audit_grant_id", "account_id", name="uq_audit_grant_account"),
    )

    # 既存ユーザーに「事業主」科目を挿入
    conn = op.get_bind()

    # equity の account_type_id を取得
    equity_type = conn.execute(
        sa.text("SELECT id FROM account_types WHERE code = 'equity'")
    ).fetchone()
    if not equity_type:
        return

    equity_type_id = equity_type[0]

    # 全ユーザーIDを取得
    users = conn.execute(sa.text("SELECT id FROM users")).fetchall()
    for (user_id,) in users:
        # 既に 3030 があればスキップ
        existing = conn.execute(
            sa.text(
                "SELECT id FROM accounts WHERE user_id = :uid AND code = '3030'"
            ),
            {"uid": user_id},
        ).fetchone()
        if existing:
            continue

        conn.execute(
            sa.text(
                "INSERT INTO accounts "
                "(user_id, account_type_id, code, name, description, tax_category, is_system, is_active, display_order) "
                "VALUES (:uid, :atid, '3030', '事業主', '監査用: 非公開科目の代替', NULL, true, true, 35)"
            ),
            {"uid": user_id, "atid": equity_type_id},
        )


def downgrade():
    op.drop_table("audit_grant_accounts")
    op.drop_table("audit_grants")
    op.drop_column("users", "user_type")
    # 事業主科目は残しても無害なので削除しない
