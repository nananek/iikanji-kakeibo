"""E4 PR-F: 証憑の死んだ平文列を DROP (#111)

E4 (#111) 証憑 E2EE 化の後片付け第 1 弾。下記 2 列はいずれも E4 の
暗号化フロー確立後は app から読み書きされなくなった「死んだ平文列」:

- voucher_audit_logs.detail
  PR-D で平文 detail の WRITE を停止し、API (api_voucher_logs) も
  encrypted_detail_blob / detail_iv の READ に移行済。`action` /
  `created_at` はフィルタ用途で平文継続 (電帳法「訂正削除の事実」を
  サーバ側でも参照可能)。本列のみ DROP する。

- vouchers.original_filename
  暗号化証憑では encrypted_meta_blob (AAD="vmeta") の中に格納される
  ため平文列は不要。app では backup dict / restore でしか参照されて
  おらず、暗号化証憑の filename は meta blob 経由で往復する。

なお当初 057 で予定していた以下は本マイグレでは **実施しない** (延期):
- vouchers.image_mime の DROP: AI クイックアクセプト経路
  (create_voucher_from_draft) が依然サーバ平文の AI 下書き画像から
  平文 voucher を生成し、配信 (serve_voucher_image) で image_mime を
  使うため。AI 下書き画像の E2EE 化が前提となる後続 PR で対応する。
- 暗号化カラム (encrypted_meta_blob / meta_iv / file_hash_plain /
  thumbnail_key / encrypted_detail_blob / detail_iv) の NOT NULL 化:
  平文 voucher が依然 NULL を持つこと、thumbnail_key / 暗号化 detail は
  設計上スパース (サムネ無し / クライアントノート専用) であることから
  NOT NULL 化できない。

downgrade は平文列を nullable で復元するのみ (値は復元されない =
実質片道のマイグレーション)。

設計書 §13 参照。

Revision ID: 057_e4_drop_voucher_plaintext
Revises: 056_e4_voucher_encrypted_columns
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa


revision = "057_e4_drop_voucher_plaintext"
down_revision = "056_e4_voucher_encrypted_columns"
branch_labels = None
depends_on = None


def upgrade():
    # ガード (#338 / E7): 未暗号化の証憑平文を破壊しない。original_filename / detail が
    # 残っているのに対応する暗号文 (encrypted_meta_blob / encrypted_detail_blob) が
    # 空/NULL の行があれば、E4 暗号化が未完了なので中断する (空テーブル・E2EE 済みは
    # 発火しない)。
    conn = op.get_bind()
    unenc_fn = conn.execute(sa.text(
        "SELECT COUNT(*) FROM vouchers "
        "WHERE original_filename IS NOT NULL AND original_filename<>'' "
        "AND (encrypted_meta_blob IS NULL OR octet_length(encrypted_meta_blob)=0)"
    )).scalar() or 0
    unenc_detail = conn.execute(sa.text(
        "SELECT COUNT(*) FROM voucher_audit_logs "
        "WHERE detail IS NOT NULL AND detail<>'' "
        "AND (encrypted_detail_blob IS NULL OR octet_length(encrypted_detail_blob)=0)"
    )).scalar() or 0
    if unenc_fn or unenc_detail:
        raise RuntimeError(
            "証憑の E4 暗号化が未完了です。平文が残っているのに暗号文が空の行を検出"
            f" (vouchers.original_filename={unenc_fn}, voucher_audit_logs.detail={unenc_detail})。"
            " 平文列を DROP する前に E4/E7 の証憑暗号化を完了させてください。"
        )

    with op.batch_alter_table("voucher_audit_logs") as batch_op:
        batch_op.drop_column("detail")

    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.drop_column("original_filename")


def downgrade():
    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.add_column(
            sa.Column("original_filename", sa.String(255), nullable=True),
        )

    with op.batch_alter_table("voucher_audit_logs") as batch_op:
        batch_op.add_column(
            sa.Column("detail", sa.Text(), nullable=True),
        )
