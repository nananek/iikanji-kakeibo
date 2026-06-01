"""E2EE Phase E5 (#112) PR-A: users に X25519 秘密鍵 (MK ラップ) 列を追加

監査連携 (§14) では owner / auditor 双方が X25519 鍵ペアを持ち、相手の公開鍵で
スナップショット (audit_packages) / 修正案 (audit_responses) を HPKE 暗号化する。

公開鍵 `users.public_key` は migration 046 で追加済み (平文保管)。本マイグレーションは
対になる**秘密鍵**の保管列を追加する。秘密鍵はクライアントが MK で AES-GCM 暗号化
(他の E2EE レコードと同じパターン) してアップロードし、サーバは平文を一切持たない。

- encrypted_private_key: pkcs8 X25519 秘密鍵を MK でラップした暗号文 (ciphertext + tag)
- private_key_iv:        AES-GCM IV (12B)

設計書 §14 は「秘密鍵は wrapped_keys に保管」と記すが、wrapped_keys は認証要素で
MK 本体をラップするテーブル (CHECK/UNIQUE/credential 依存) でスキーマが合わないため、
public_key と並ぶ users 列に置く (E5 PR-A の設計判断)。

Revision ID: 061_users_x25519_private_key
Revises: 060_drop_voucher_image_mime
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa


revision = "061_users_x25519_private_key"
down_revision = "060_drop_voucher_image_mime"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("encrypted_private_key", sa.LargeBinary(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("private_key_iv", sa.LargeBinary(), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("private_key_iv")
        batch_op.drop_column("encrypted_private_key")
