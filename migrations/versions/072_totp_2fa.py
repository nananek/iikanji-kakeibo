"""#385 PR-T1: TOTP 2FA secret 保管基盤の列・テーブルを追加

設計書 docs/v5-e2ee/login-derived-mk.md §3.6。TOTP 2 要素認証 (opt-in) のため、サーバ側で
at-rest 暗号化した TOTP secret と、バックアップコード (SHA-256 ハッシュ) を保管する。

`users` に追加:
- `totp_secret_encrypted` (BYTEA): TOTP secret (20B) を AES-256-GCM (totp_enc_key, aad=user_id)
  で暗号化した暗号文+tag (36B 想定)。totp_enc_key = HKDF(LOGIN_SERVER_SECRET, "iikanji-totp-enc-v1")。
- `totp_secret_iv` (BYTEA 12B)
- `totp_enabled` (BOOLEAN NOT NULL default false): verify-before-enable。確認コードが通るまで false。
- `totp_confirmed_at` (TIMESTAMPTZ nullable)
- `totp_last_used_step` (BIGINT nullable): replay 対策。最後に検証成功した TOTP step を記録し
  同一 step の再利用を拒否する (§3.6.4)。

新テーブル `totp_backup_codes`: TOTP デバイス紛失時のワンタイムコード (1 回限り使用)。
- `code_hash` (SHA-256 hexdigest) / `code_prefix` (表示用) / `used_at` (使用済みマーク)。

いずれも nullable 追加 / NOT NULL + server_default 付き追加 / 新規テーブルのため既存行・
downgrade ともに安全。

Revision ID: 072_totp_2fa
Revises: 071_recovery_reset_columns
Create Date: 2026-06-07
"""

from alembic import op
import sqlalchemy as sa


revision = "072_totp_2fa"
down_revision = "071_recovery_reset_columns"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("totp_secret_encrypted", sa.LargeBinary(), nullable=True))
        b.add_column(sa.Column("totp_secret_iv", sa.LargeBinary(), nullable=True))
        b.add_column(
            sa.Column(
                "totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        b.add_column(
            sa.Column("totp_confirmed_at", sa.DateTime(timezone=True), nullable=True)
        )
        b.add_column(sa.Column("totp_last_used_step", sa.BigInteger(), nullable=True))

    op.create_table(
        "totp_backup_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("code_prefix", sa.String(20), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_table("totp_backup_codes")
    with op.batch_alter_table("users") as b:
        b.drop_column("totp_last_used_step")
        b.drop_column("totp_confirmed_at")
        b.drop_column("totp_enabled")
        b.drop_column("totp_secret_iv")
        b.drop_column("totp_secret_encrypted")
