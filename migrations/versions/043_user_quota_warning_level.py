"""User に last_quota_warning_level カラムを追加 (Phase 6 #71 quota_warning)

ストレージ使用率の閾値超過時 (80% / 95%) に `quota_warning` メールを
送るが、同じ閾値帯にいる間は再送しない (重複通知防止)。直近に送信
した警告レベルを `users.last_quota_warning_level` に記録する。

- NULL: 通常状態 (まだ警告を送信していない、または 70% 未満まで回復)
- "warning": 80% 帯に達した時点で送信、80%-95% 維持中は再送しない
- "critical": 95% 帯に達した時点で送信、それ以降は再送しない

70% 未満に回復した時点で NULL にリセットし、次回 80% 到達で再通知できる
ヒステリシス設計。

Revision ID: 043_user_quota_warning_level
Revises: 042_voucher_audit_log_nullable
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "043_user_quota_warning_level"
down_revision = "042_voucher_audit_log_nullable"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("last_quota_warning_level", sa.String(20), nullable=True),
    )


def downgrade():
    op.drop_column("users", "last_quota_warning_level")
