"""E4 PR-G: 証憑 AAD を安定識別子 (aad_id) に束縛 (#111)

E4 (#111) 証憑 E2EE の AAD は当初 `voucher_id` (整数 PK) を束縛していたが、
backup/restore は復元時に全行を delete → 新 PK で再挿入する (PK 再採番) ため、
voucher_id が変わり **AAD 不一致でクライアント復号不能**になる問題があった
(je/jel/me は user_id のみ束縛で再採番に耐えるが、voucher は voucher_id 束縛
だった)。

対策 (Option C): voucher_id とは独立した安定識別子 `aad_id` を導入し、AAD を
voucher_id ではなく aad_id に束縛する。`aad_id` はサーバが init 時に生成する
63bit ランダム整数で、backup/restore で PK が再採番されても保持される。これに
より復元後もクライアントは同じ AAD を再構築でき復号できる。voucher 単位で一意
(`UNIQUE(user_id, aad_id)`) なため、voucher 間 ciphertext swap の検知能力も
voucher_id 束縛と同等に保たれる。

ADD COLUMN:
- vouchers.aad_id (BigInteger, NULL 許容): E2EE 証憑のみセット。レガシー平文
  証憑 (AI 下書き由来) は NULL。`UNIQUE(user_id, aad_id)` (Postgres は NULL を
  distinct 扱いするためレガシー NULL 行の併存可)。

設計書 §12.2 / §13.2 参照。

Revision ID: 058_e4_voucher_aad_id
Revises: 057_e4_drop_voucher_plaintext
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa


revision = "058_e4_voucher_aad_id"
down_revision = "057_e4_drop_voucher_plaintext"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.add_column(
            sa.Column("aad_id", sa.BigInteger(), nullable=True),
        )
        batch_op.create_unique_constraint(
            "uq_vouchers_user_aad_id", ["user_id", "aad_id"],
        )


def downgrade():
    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.drop_constraint("uq_vouchers_user_aad_id", type_="unique")
        batch_op.drop_column("aad_id")
