"""パスキー専用モードとリカバリコードのカラムを物理 DROP

TOTP 2FA 導入 (046) に伴い、パスキー専用ログインモードと非常用リカバリ
コードを廃止する。パスキー紛失時の復旧経路は TOTP が担う。全ユーザーは
パスワードを持つ (password_hash NOT NULL) ため、passkey_only_login の廃止で
ロックアウトは発生しない。

revision ID は alembic_version.version_num (varchar(32)) に収まる長さにする。

Revision ID: 047_drop_passkey_recovery
Revises: 046_totp_columns
Create Date: 2026-06-07
"""

from alembic import op
import sqlalchemy as sa


revision = "047_drop_passkey_recovery"
down_revision = "046_totp_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("users", "recovery_code_used_at")
    op.drop_column("users", "recovery_code_created_at")
    op.drop_column("users", "recovery_code_prefix")
    op.drop_column("users", "recovery_code_hash")
    op.drop_column("users", "passkey_only_login")


def downgrade():
    # 034 と同じ定義で復元する
    op.add_column(
        "users",
        sa.Column(
            "passkey_only_login",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("recovery_code_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("recovery_code_prefix", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("recovery_code_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("recovery_code_used_at", sa.DateTime(timezone=True), nullable=True),
    )
