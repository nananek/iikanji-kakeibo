"""AIDraft に file_size カラムを追加 (Phase 5 #70)

AIDraft 段階で StorageUsage に計上した原本画像のバイト数を保持する。
Voucher 化時に Voucher.file_size に引き継ぎ、所有権移転で計上は不変。
AIDraft 単独で削除された場合 (drafts_delete) は file_size を見て
record_delete する。

既存レコードは NULL のまま。整合性監査バッチ (後続 PR) でストレージから
実測して埋める想定。NULL の AIDraft は削除時に record_delete されない
ため、Phase 5 計上開始前のドラフトに関しては StorageUsage を進行的に
正しい値へ収束させる。

Revision ID: 040_ai_draft_file_size
Revises: 039_voucher_file_size
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "040_ai_draft_file_size"
down_revision = "039_voucher_file_size"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ai_drafts",
        sa.Column("file_size", sa.BigInteger(), nullable=True),
    )


def downgrade():
    op.drop_column("ai_drafts", "file_size")
