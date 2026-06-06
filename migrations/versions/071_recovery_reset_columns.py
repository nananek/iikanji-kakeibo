"""#385 PR-4b-1: リカバリリセット用の列を追加

設計書 docs/v5-e2ee/login-derived-mk.md §3.4.1。リカバリシードを「フル復旧因子」化
するため、シードにサーバ側 verifier を持たせる + リセット/パスワード変更時のセッション
失効カウンタを追加する。

- `recovery_seed_server_hash` (BYTEA 32B nullable):
    HMAC-SHA256(LOGIN_SERVER_SECRET, "recovery-hash" || 0x00 || recovery_verifier)。
    recovery_verifier = HKDF(seed_bytes, info="iikanji-recovery-login-v1")。DB 流出時も
    シード平文/verifier を得られない (login_server_hash と同方針)。旧ウィザードで作成した
    既存ユーザーは NULL (後埋め導線で確立)。
- `session_token_version` (INTEGER NOT NULL default 0):
    Flask-Login `get_id()` に焼き込み、reset / パスワード変更でインクリメントすると
    旧セッション Cookie が `load_user` の照合で失効する (§3.4.1 セッション失効)。
    既存 Cookie は version 情報を持たないため `load_user` が 0 とみなす → default 0 で
    後方互換 (既存ログインセッションを切らない)。

いずれも nullable 追加 / NOT NULL + server_default 付き追加のため既存行・downgrade
ともに安全。

Revision ID: 071_recovery_reset_columns
Revises: 070_login_derived_mk
Create Date: 2026-06-07
"""

from alembic import op
import sqlalchemy as sa


revision = "071_recovery_reset_columns"
down_revision = "070_login_derived_mk"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as b:
        b.add_column(
            sa.Column("recovery_seed_server_hash", sa.LargeBinary(), nullable=True)
        )
        b.add_column(
            sa.Column(
                "session_token_version",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade():
    with op.batch_alter_table("users") as b:
        b.drop_column("session_token_version")
        b.drop_column("recovery_seed_server_hash")
