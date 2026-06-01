"""E5 PR-1: AI 下書き画像の E2EE 化に向けたカラム追加 (#111)

E4 で証憑 (vouchers) に適用した「BLOB + IV 別カラム保管 + クライアント完結
AES-256-GCM 暗号化 + aad_id 安定識別子」パターンを、AI 証憑仕訳の下書き
(ai_drafts) にも適用する第 1 段階。本マイグレでは dual-write 期間を開くために
カラムを追加するのみ (既存行は NULL のまま)。

AI 下書き画像は現状サーバ平文保存だが、E2EE 化することで:
- 画像本体をクライアント暗号化してアップロード (vouchers と同形式)
- `create_voucher_from_draft` で下書き → 証憑へ移行する際、暗号化成果物と
  `aad_id` をそのまま引き継ぐ (再暗号化なしに AAD を維持)
これにより最終的に `vouchers.image_mime` の DROP (PR-F2) が可能になる。

ADD COLUMN (いずれも NULL 許容):
- ai_drafts:
  - encrypted_meta_blob (LargeBinary): original_filename + image_mime 等の
    メタ情報を JSON 化して AES-GCM 暗号化した blob (AAD = "vmeta")。
    voucher と同じ AAD ドメインを使い、下書き → 証憑移行時に再暗号化を不要にする。
  - meta_iv (LargeBinary): meta blob の 12B IV
  - file_hash_plain (String 64): SHA-256(平文画像)。クライアントが計算して送信
    (電帳法 Q11 ハイブリッドの平文側)。既存 file_hash は暗号文ハッシュとして継続。
  - thumbnail_key (String 255): クライアント生成サムネイル (暗号文) の
    ストレージキー。
  - aad_id (BigInteger): E2EE 下書きの AAD 束縛用 63bit ランダム整数。
    `UNIQUE(user_id, aad_id)`。レガシー平文下書きは NULL (Postgres は NULL を
    distinct 扱いするため併存可)。下書き → 証憑移行時に Voucher.aad_id へ
    そのまま引き継ぐ。

設計書 §12.6 / §13.6 参照。

Revision ID: 059_ai_draft_e2ee_columns
Revises: 058_e4_voucher_aad_id
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa


revision = "059_ai_draft_e2ee_columns"
down_revision = "058_e4_voucher_aad_id"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ai_drafts") as batch_op:
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
        batch_op.add_column(
            sa.Column("aad_id", sa.BigInteger(), nullable=True),
        )
        batch_op.create_unique_constraint(
            "uq_ai_drafts_user_aad_id", ["user_id", "aad_id"],
        )


def downgrade():
    with op.batch_alter_table("ai_drafts") as batch_op:
        batch_op.drop_constraint("uq_ai_drafts_user_aad_id", type_="unique")
        batch_op.drop_column("aad_id")
        batch_op.drop_column("thumbnail_key")
        batch_op.drop_column("file_hash_plain")
        batch_op.drop_column("meta_iv")
        batch_op.drop_column("encrypted_meta_blob")
