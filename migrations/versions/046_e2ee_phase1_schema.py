"""E2EE Phase E1: wrapped_keys テーブル + users 拡張

Master Key を 3 認証要素 (Passkey PRF / passphrase / recovery_seed) で
ラップして保管するための `wrapped_keys` テーブルを新設し、users に
鍵管理基盤関連カラムを追加する。

設計書: docs/v5-e2ee/index.md §10

users への追加カラム:
- is_active (default True): 鍵未設定ユーザーの強制ロック用 (§16.5)
- migration_temp_mk: 一斉移行中の一時 MK 保管 (§13.9 / §16.4)
- public_key: X25519 公開鍵 (E5 監査連携で使用、§14.4 参照)
- mk_rotation_state: ローテーション進捗 (§10.5)

wrapped_keys: §10.1 のスキーマ通り

Revision ID: 046_e2ee_phase1_schema
Revises: 045_voucher_active_partial_index
Create Date: 2026-05-22
"""

import sqlalchemy as sa
from alembic import op


revision = "046_e2ee_phase1_schema"
down_revision = "045_voucher_active_partial_index"
branch_labels = None
depends_on = None


def upgrade():
    # users 拡張
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "users",
        sa.Column("migration_temp_mk", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("public_key", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("mk_rotation_state", sa.JSON(), nullable=True),
    )

    # wrapped_keys テーブル
    op.create_table(
        "wrapped_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("webauthn_credential_id", sa.Integer(), nullable=True),
        sa.Column("wrapped_master_key", sa.LargeBinary(), nullable=False),
        sa.Column("wrap_iv", sa.LargeBinary(), nullable=False),
        sa.Column("salt", sa.LargeBinary(), nullable=True),
        sa.Column("kdf_params", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label", sa.String(100), nullable=True),
        sa.CheckConstraint(
            "method IN ('passkey_prf', 'passphrase', 'recovery_seed')",
            name="ck_wrapped_keys_method",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["webauthn_credential_id"], ["webauthn_credentials.id"],
            ondelete="CASCADE",
        ),
    )

    op.create_index(
        "ix_wrapped_keys_user_id", "wrapped_keys", ["user_id"]
    )
    op.create_index(
        "ix_wrapped_keys_webauthn_credential_id",
        "wrapped_keys",
        ["webauthn_credential_id"],
    )
    # passkey_prf 行は (user_id, credential_id) 単位で UNIQUE
    op.execute(
        "CREATE UNIQUE INDEX uq_wrapped_keys_passkey "
        "ON wrapped_keys (user_id, method, webauthn_credential_id) "
        "WHERE webauthn_credential_id IS NOT NULL"
    )
    # passphrase / recovery_seed 行は (user_id, method) 単位で UNIQUE
    op.execute(
        "CREATE UNIQUE INDEX uq_wrapped_keys_passphrase_recovery "
        "ON wrapped_keys (user_id, method) "
        "WHERE webauthn_credential_id IS NULL"
    )


def downgrade():
    op.drop_index("uq_wrapped_keys_passphrase_recovery", table_name="wrapped_keys")
    op.drop_index("uq_wrapped_keys_passkey", table_name="wrapped_keys")
    op.drop_index(
        "ix_wrapped_keys_webauthn_credential_id", table_name="wrapped_keys"
    )
    op.drop_index("ix_wrapped_keys_user_id", table_name="wrapped_keys")
    op.drop_table("wrapped_keys")
    op.drop_column("users", "mk_rotation_state")
    op.drop_column("users", "public_key")
    op.drop_column("users", "migration_temp_mk")
    op.drop_column("users", "is_active")
