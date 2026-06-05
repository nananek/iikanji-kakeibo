"""E5 PR-5 (= PR-F2): vouchers.image_mime 列を DROP (#111)

AI 下書き画像の E2EE 化 (PR-1〜4) により、証憑画像は:
- E2EE 証憑: 実 MIME は encrypted_meta_blob 内 (クライアントのみ復号可)。
  image_mime 列はプレースホルダ (application/octet-stream) しか入っていなかった。
- レガシー平文証憑 (client-py 等の AI クイックアクセプト由来): 配信を
  image_mime 非依存 (octet-stream + ブラウザの content-sniff) に切替えたため
  (PR-5 のコード変更)、列はもう読まれない。

これにより `vouchers.image_mime` は全経路で読まれなくなったので物理 DROP する。
平文画像本体はストレージに残り、octet-stream で配信され続ける (ブラウザが
画像として表示)。

注意: `ai_drafts.image_mime` は DROP しない (client-py の平文アップロード経路で
まだ使用。下書き配信も同様に octet-stream 化済みだが、列自体は温存)。

downgrade はカラムを再追加するが、元の MIME 値は復元できない
(application/octet-stream のプレースホルダで埋める。057 と同じ運用注意)。

Revision ID: 060_drop_voucher_image_mime
Revises: 059_ai_draft_e2ee_columns
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa


revision = "060_drop_voucher_image_mime"
down_revision = "059_ai_draft_e2ee_columns"
branch_labels = None
depends_on = None


def upgrade():
    # ガード (#338 / E7): 未暗号化の証憑が残るうちに image_mime を DROP しない。
    # E2EE 証憑では実 MIME は encrypted_meta_blob 内にあり image_mime はプレース
    # ホルダだが、v4.0.0 など未移行の証憑は image_mime に実 MIME を持ち
    # encrypted_meta_blob が空。その状態で DROP すると MIME が失われるため中断する
    # (空テーブル・E2EE 済みは発火しない。055 で先に止まる想定の defense-in-depth)。
    conn = op.get_bind()
    unenc = conn.execute(sa.text(
        "SELECT COUNT(*) FROM vouchers "
        "WHERE image_mime IS NOT NULL "
        "AND (encrypted_meta_blob IS NULL OR octet_length(encrypted_meta_blob)=0)"
    )).scalar() or 0
    if unenc:
        raise RuntimeError(
            f"未暗号化の証憑が {unenc} 件あります (image_mime あり / encrypted_meta_blob 空)。"
            " image_mime を DROP する前に E4/E7 の証憑暗号化を完了させてください。"
        )

    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.drop_column("image_mime")


def downgrade():
    # 元の MIME は復元不能。NOT NULL を満たすためプレースホルダで埋める。
    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.add_column(
            sa.Column(
                "image_mime",
                sa.String(50),
                nullable=False,
                server_default="application/octet-stream",
            ),
        )
    # server_default は復元用の一時的措置なので外す (アプリは値を設定する)。
    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.alter_column("image_mime", server_default=None)
