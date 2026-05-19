"""User.accepted_terms_version カラムを追加

Phase 1 #66 の規約同意管理。利用規約改訂時の再同意フローで
`CURRENT_TERMS_VERSION` と照合する。新規登録時は登録フォームの同意
チェックボックス送信時に現行バージョンを書き込み、既存ユーザーは
NULL のまま (初回ログイン時に再同意フローへ誘導される)。

Revision ID: 037_accepted_terms_version
Revises: 036_ai_usage_log
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "037_accepted_terms_version"
down_revision = "036_ai_usage_log"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("accepted_terms_version", sa.String(length=20), nullable=True),
    )


def downgrade():
    op.drop_column("users", "accepted_terms_version")
