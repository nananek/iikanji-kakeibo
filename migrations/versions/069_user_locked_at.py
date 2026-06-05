"""E7 #114 PR-4b: 鍵未設定ロック (§16.5) 用に users.locked_at を追加

メンテナンスウィンドウ後に鍵を設定しないユーザーを `is_active=False` でロック
する運用フロー (§16.5) のために、ロックを付与した時刻を記録する列を追加する。

- `migration-lock-stale` (PR-4b-2) がロック時に `is_active=False` と同時に
  `locked_at=now` をセットする。
- `migration-purge-locked` (PR-4b-3) が `locked_at` から 60 日経過した鍵未設定
  ユーザーを自動退会の候補にする。
- ロック解決 (鍵設定完了) 時に `is_active=True` へ戻すと同時に `locked_at=NULL`
  にクリアする (migration_lock_gate の自己回復)。

nullable で追加するだけのため、既存行・downgrade ともに安全。

Revision ID: 069_user_locked_at
Revises: 068_e3f_jel_drop_plaintext
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa


revision = "069_user_locked_at"
down_revision = "068_e3f_jel_drop_plaintext"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as b:
        b.drop_column("locked_at")
