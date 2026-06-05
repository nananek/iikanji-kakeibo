"""E4 PR-A: 証憑の E2EE 化に向けたカラム追加 (#111)

Phase E4 (#111) のスキーマ準備フェーズ。E3 で確立した
「BLOB + IV 別カラム保管 + クライアント完結 AES-256-GCM 暗号化」パターンを
証憑画像 (vouchers) に適用する第 1 段階。本マイグレでは dual-write 期間を
開くためにカラムを追加するのみ (既存行は NULL のまま)。

ADD COLUMN (いずれも NULL 許容):
- vouchers:
  - encrypted_meta_blob (LargeBinary): original_filename + image_mime 等の
    メタ情報を JSON 化して AES-GCM 暗号化した blob (AAD = "vmeta")
  - meta_iv (LargeBinary): meta blob の 12B IV
  - file_hash_plain (String 64): SHA-256(平文画像)。クライアントが計算して送信
    し、復号後に再計算して改ざん検出する (電帳法 Q11 ハイブリッドの平文側)。
    既存の file_hash 列は暗号文ハッシュ (file_hash_cipher 相当) として継続。
  - thumbnail_key (String 255): クライアント生成サムネイル (暗号文) の
    ストレージキー。サーバ Pillow 生成 (_thumb.jpg) は E4 後半で廃止。
- voucher_audit_logs:
  - encrypted_detail_blob (LargeBinary): detail (JSON) を暗号化した blob
    (AAD = "valog")
  - detail_iv (LargeBinary): detail blob の 12B IV

旧平文列 (original_filename / image_mime、voucher_audit_logs.detail) の DROP と
file_hash の cipher hash への意味づけ確定は E4 後半 (057) で実施。

設計書 §13.1 / §13.2 / §13.4 参照。

Revision ID: 056_e4_voucher_encrypted_columns
Revises: 055_e3f_drop_plaintext_columns
Create Date: 2026-05-31
"""

from alembic import op
import sqlalchemy as sa


revision = "056_e4_voucher_encrypted_columns"
down_revision = "055_e3f_drop_plaintext_columns"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.add_column(
            sa.Column("encrypted_meta_blob", sa.LargeBinary(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("meta_iv", sa.LargeBinary(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("file_hash_plain", sa.String(64), nullable=True),
        )
        batch_op.add_column(
            sa.Column("thumbnail_key", sa.String(255), nullable=True),
        )
        # #114 (E7): aad_id を 056 で追加する (元は 058)。E7 のサーバ側証憑暗号化は
        # original_filename (057 で DROP) と aad_id の両方が必要なため、057 より前に
        # aad_id を用意しておく。058 は冪等化して二重追加を避ける。
        batch_op.add_column(
            sa.Column("aad_id", sa.BigInteger(), nullable=True),
        )
        batch_op.create_unique_constraint(
            "uq_vouchers_user_aad_id", ["user_id", "aad_id"],
        )

    with op.batch_alter_table("voucher_audit_logs") as batch_op:
        batch_op.add_column(
            sa.Column("encrypted_detail_blob", sa.LargeBinary(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("detail_iv", sa.LargeBinary(), nullable=True),
        )


def downgrade():
    with op.batch_alter_table("voucher_audit_logs") as batch_op:
        batch_op.drop_column("detail_iv")
        batch_op.drop_column("encrypted_detail_blob")

    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.drop_constraint("uq_vouchers_user_aad_id", type_="unique")
        batch_op.drop_column("aad_id")
        batch_op.drop_column("thumbnail_key")
        batch_op.drop_column("file_hash_plain")
        batch_op.drop_column("meta_iv")
        batch_op.drop_column("encrypted_meta_blob")
