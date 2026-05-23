"""E2 Phase E2-a: UserAIConfig に E2EE 用カラム追加

設計書 §11.4 (マイグレーション戦略) Phase E2-a:

1. ADD COLUMN api_key_blob bytea NULL      — クライアント暗号化済 AES-GCM 暗号文
2. ADD COLUMN api_key_iv   bytea NULL      — AES-GCM IV (12B)
3. ADD COLUMN migrated_at  timestamptz NULL — migrate-key 1 回限り判定用
4. ALTER api_key_encrypted DROP NOT NULL    — 移行完了後に NULL クリアするため

旧 api_key_encrypted カラム自体は Phase E2-b (旧データ全消去後の別マイグレーション)
で DROP する。E2-a 時点では互換のため残す。

Revision ID: 047_ai_config_e2ee_columns
Revises: 046_e2ee_phase1_schema
Create Date: 2026-05-23
"""

import sqlalchemy as sa
from alembic import op


revision = "047_ai_config_e2ee_columns"
down_revision = "046_e2ee_phase1_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_ai_configs",
        sa.Column("api_key_blob", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "user_ai_configs",
        sa.Column("api_key_iv", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "user_ai_configs",
        sa.Column("migrated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 既存 api_key_encrypted の NOT NULL 制約を外す (移行完了後に NULL 化するため)。
    # SQLite は ALTER COLUMN を直接サポートしないため batch_alter_table で代替。
    with op.batch_alter_table("user_ai_configs") as batch_op:
        batch_op.alter_column("api_key_encrypted", nullable=True)


def downgrade():
    # NULL の値を持つ行があると NOT NULL 復帰時に失敗するため、
    # 復帰時はダミーバイト 1 を埋める (E2-b 経由してない状態への戻り)。
    # `X'00'` は SQLite hex リテラル構文で PostgreSQL では非互換のため、
    # パラメタライズ済 SQL で DB 非依存にする (op.execute 経由)。
    op.execute(
        sa.text(
            "UPDATE user_ai_configs SET api_key_encrypted = :dummy "
            "WHERE api_key_encrypted IS NULL"
        ).bindparams(sa.bindparam("dummy", b"\x00"))
    )
    with op.batch_alter_table("user_ai_configs") as batch_op:
        batch_op.alter_column("api_key_encrypted", nullable=False)

    op.drop_column("user_ai_configs", "migrated_at")
    op.drop_column("user_ai_configs", "api_key_iv")
    op.drop_column("user_ai_configs", "api_key_blob")
