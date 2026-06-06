"""#385 PR-2: ログイン派生 MK 用の認証列を追加し password_hash を nullable 化

設計書 docs/v5-e2ee/login-derived-mk.md §7.1。ログインパスワードから MK を
派生する方式 (HKDF split) のため、werkzeug `password_hash` に代えて以下を置く:

- `login_server_hash`  : HMAC-SHA256(LOGIN_SERVER_SECRET, "login-hash"||0x00||
                         login_verifier) を保存 (BYTEA 32B)
- `login_salt`         : Argon2id の per-user salt (BYTEA 16B)
- `login_kdf_params`   : Argon2id パラメータ {memory, iterations, parallelism} (JSON)
- `login_secret_version`: LOGIN_SERVER_SECRET 遅延ローテーション用 (SMALLINT)

`password_hash` は **段階的撤去** (自然移行のため即 drop しない):
本マイグレで nullable=True 化し、各 v4 ユーザーの初回ログイン移行 finalize 時に
NULL クリアする。全ユーザー移行完了後に後続マイグレで物理 DROP する。
`login_salt IS NOT NULL` を移行済み判定に使う。

すべて nullable 追加 + nullable 緩和のため既存行・downgrade ともに安全。

Revision ID: 070_login_derived_mk
Revises: 069_user_locked_at
Create Date: 2026-06-06
"""

from alembic import op
import sqlalchemy as sa


revision = "070_login_derived_mk"
down_revision = "069_user_locked_at"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("login_server_hash", sa.LargeBinary(), nullable=True))
        b.add_column(sa.Column("login_salt", sa.LargeBinary(), nullable=True))
        b.add_column(sa.Column("login_kdf_params", sa.JSON(), nullable=True))
        b.add_column(
            sa.Column("login_secret_version", sa.SmallInteger(), nullable=True)
        )
        # 自然移行のため password_hash を即 drop せず nullable 化する。
        # 移行 finalize 時に NULL クリア、後続マイグレで物理 DROP。
        b.alter_column("password_hash", existing_type=sa.String(256), nullable=True)


def downgrade():
    # password_hash に NULL が入った行があると NOT NULL へ戻せないため、
    # downgrade 前に NULL 行を空文字で埋める (移行済み行のロールバック安全性)。
    op.execute("UPDATE users SET password_hash = '' WHERE password_hash IS NULL")
    with op.batch_alter_table("users") as b:
        b.alter_column("password_hash", existing_type=sa.String(256), nullable=False)
        b.drop_column("login_secret_version")
        b.drop_column("login_kdf_params")
        b.drop_column("login_salt")
        b.drop_column("login_server_hash")
