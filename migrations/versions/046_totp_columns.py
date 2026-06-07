"""TOTP 2要素認証用カラムを users に追加

opt-in の TOTP 2FA (RFC 6238)。secret は SECRET_KEY 由来の Fernet で
暗号化して保管する。totp_last_used_step はログイン時のリプレイ防止用。
バックアップコードは持たない (パスキーログインが復旧経路を担う)。

Revision ID: 046_totp_columns
Revises: 045_voucher_active_partial_index
Create Date: 2026-06-07
"""

from alembic import op
import sqlalchemy as sa


revision = "046_totp_columns"
down_revision = "045_voucher_active_partial_index"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("totp_secret_encrypted", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "totp_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("totp_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("totp_last_used_step", sa.BigInteger(), nullable=True),
    )


def downgrade():
    op.drop_column("users", "totp_last_used_step")
    op.drop_column("users", "totp_confirmed_at")
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret_encrypted")
