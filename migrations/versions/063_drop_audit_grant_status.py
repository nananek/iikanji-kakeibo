"""E5 PR-8: audit_grants.status / submitted_at 列を DROP (#112)

旧リアルタイム代理閲覧 (acting_as_user_id セッション方式) の Lv2 提出フロー
専用フィールド。代理閲覧の撤去 (PR-1〜7) により全コード経路から参照が消えた
(grant の有効/無効判定は revoked_at が唯一のマーカー、§14.10)。物理 DROP する。

AuditGrant / AuditGrantAccount テーブル自体と revoked_at は非同期スナップショット
ワークフロー (audit_packages) で引き続き使用するため温存する。

downgrade は列を再追加するが、提出状態は復元できない (status は default "draft"
のプレースホルダ、submitted_at は NULL)。

Revision ID: 063_drop_audit_grant_status
Revises: 062_audit_packages
Create Date: 2026-06-02
"""

from alembic import op
import sqlalchemy as sa


revision = "063_drop_audit_grant_status"
down_revision = "062_audit_packages"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("audit_grants") as batch_op:
        batch_op.drop_column("submitted_at")
        batch_op.drop_column("status")


def downgrade():
    # 提出状態は復元不能。NOT NULL を満たすため default "draft" で埋める。
    with op.batch_alter_table("audit_grants") as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(10),
                nullable=False,
                server_default="draft",
            ),
        )
        batch_op.add_column(
            sa.Column("submitted_at", sa.DateTime(), nullable=True),
        )
    # server_default は復元用の一時措置なので外す。
    with op.batch_alter_table("audit_grants") as batch_op:
        batch_op.alter_column("status", server_default=None)
