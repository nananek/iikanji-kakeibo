"""#385 PR-T4-drop: passkey_only_login 列を物理 DROP

設計書 docs/v5-e2ee/login-derived-mk.md §3.6.6。パスキー専用モードは PR-T4 (#409) で
振る舞い・UI・enforcement を撤去済み。本マイグレで列を物理 DROP し、全ユーザーに
パスワード必須 + 2FA = 「Passkey or TOTP」の方針へ完全移行する。

前提 (本番適用時に確認): `SELECT COUNT(*) FROM users WHERE passkey_only_login=TRUE` が 0、
または passkey_only ユーザーがパスワード経路 (password_hash 保持 / リカバリシードでの
パスワード設定) へ移行済みであること (#409 review 申し送り)。列を落とすとフラグは失われ、
password_hash を持つ旧 passkey_only ユーザーはパスワードログインが有効化される (意図的)。

データ損失は無い (boolean フラグのみ)。downgrade で列を復元できる (全行 False)。

Revision ID: 073_drop_passkey_only
Revises: 072_totp_2fa
Create Date: 2026-06-07
"""

from alembic import op
import sqlalchemy as sa


revision = "073_drop_passkey_only"
down_revision = "072_totp_2fa"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as b:
        b.drop_column("passkey_only_login")


def downgrade():
    with op.batch_alter_table("users") as b:
        b.add_column(
            sa.Column(
                "passkey_only_login",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
