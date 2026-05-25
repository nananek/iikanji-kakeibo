"""E2 PR-E-b-2: UserAIConfig から Fernet 関連カラムを DROP

E2-E-b-1 でサーバ側 Fernet 経路 (encrypt/decrypt/_get_ai_config/
migrate-key endpoint) を全廃したため、DB スキーマからも下記カラムを削除:

- api_key_encrypted (LargeBinary) — 旧 Fernet サーバ暗号化 API キー
- migrated_at (DateTime) — migrate-key endpoint 1 回限り判定用

カラム削除後、API キー保管は api_key_blob + api_key_iv (クライアント側
MK で AES-256-GCM 暗号化、サーバ復号不可) のみになる。

⚠️ 既存ユーザーで api_key_blob/iv 未保存 (旧 Fernet のみで E2EE 未移行)
の場合、本マイグレ後は API キーが事実上失われる。設定画面で API キー
再入力を促す UI は E2-E-b-1 までで整備済 (is_e2ee=False → 「E2EE 形式で
再登録」warning + フォーム無効化)。

Revision ID: 049_drop_fernet_columns
Revises: 048_drop_auto_import_tables
Create Date: 2026-05-25
"""

from alembic import op
import sqlalchemy as sa


revision = "049_drop_fernet_columns"
down_revision = "048_drop_auto_import_tables"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_ai_configs") as batch_op:
        batch_op.drop_column("api_key_encrypted")
        batch_op.drop_column("migrated_at")


def downgrade():
    # 復帰時は両カラムを NULL 許容で戻す (047 と同じ状態に)。
    # Fernet 暗号化値の復元は不可能なので、巻き戻し後のユーザーは設定画面で
    # 再入力する必要がある (api_key_encrypted は NULL のまま残る)。
    with op.batch_alter_table("user_ai_configs") as batch_op:
        batch_op.add_column(
            sa.Column("api_key_encrypted", sa.LargeBinary(), nullable=True),
        )
        batch_op.add_column(
            sa.Column(
                "migrated_at", sa.DateTime(timezone=True), nullable=True,
            ),
        )
