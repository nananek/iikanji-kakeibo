"""#338 item8 (Phase 8): journal_entry_lines の平文メタ列と FK を物理 DROP

#338 方針B (仕訳明細の金額・科目コードを真に E2EE 化し DB 平文ゼロを達成) の
最終フェーズ。Phase 5a-c で平文 account_code / debit_amount / credit_amount の
READ (item4 + Phase R) と WRITE/wire (5b/5c) を全廃し、067 で 3 列を nullable 化
した。本マイグレで以下を物理 DROP する:

- 複合 FK fk_jel_account (account_user_id, account_code) → accounts(user_id, code)
- journal_entry_lines.account_code
- journal_entry_lines.debit_amount
- journal_entry_lines.credit_amount

行の科目・金額は encrypted_blob に収録済。貸借一致・科目存在の検査はクライアント
(復号時) + 監査時検査の責務へ全面委譲する (設計書 §12.11)。完了後、脅威モデル §1/§4
の「仕訳の金額・科目コードを守る」が実装と一致し、真の「DB 平文ゼロ」を達成する。

## upgrade 前提のプリチェック (不可逆 DROP の安全網)

item1 以前にサーバが平文 SQL SUM で生成した旧 closing (損益振替) 仕訳は、行レベル
では平文金額のみを持ち encrypted_blob が空センチネル (b"") のまま残っている場合が
ある。この状態で平文列を DROP すると、確定済み年度の損益振替の金額が復元不能に
なる。よって upgrade 冒頭で「is_closing かつ encrypted_blob が空」の journal_entry が
1 件でも残っていれば中断し、`設定 → 月次確定` の「すべて再暗号化」(reencrypt-closing)
を先に実行するよう促す。

注: 「平文列に非 NULL 値が残る = write 未停止」は検出しない。5b/5c の write 停止前に
正当に作られた既存行も平文を持つため、それを根拠にブロックすると誤検知になる。
プリチェックは旧 closing 未移行の 1 点に絞る。

downgrade は 3 列を nullable で復元するのみ (平文データは失われており値は復元され
ない。FK も再作成しない = 実質片道のマイグレーション)。

Revision ID: 068_e3f_jel_drop_plaintext
Revises: 067_e3f_line_plaintext_nullable
Create Date: 2026-06-05
"""

from alembic import op
import sqlalchemy as sa


revision = "068_e3f_jel_drop_plaintext"
down_revision = "067_e3f_line_plaintext_nullable"
branch_labels = None
depends_on = None


def upgrade():
    # プリチェック: 旧 closing (is_closing かつ encrypted_blob が空センチネル) が
    # 残っていたら不可逆 DROP を中断する。`settings.py:_old_closing_years` と同条件。
    conn = op.get_bind()
    legacy = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM journal_entries "
            "WHERE is_closing AND encrypted_blob = :empty"
        ).bindparams(empty=b"")
    ).scalar()
    if legacy:
        raise RuntimeError(
            f"未移行の旧 closing (損益振替) 仕訳が {legacy} 件残っています。"
            "平文列を DROP する前に、各利用者が `設定 → 月次確定` の"
            "「すべて再暗号化」を実行して旧 closing を暗号化版へ移行してください "
            "(#338 item8 のプリチェック。未移行のまま DROP すると確定済み年度の"
            "損益振替金額が復元不能になります)。"
        )

    # 複合 FK を先に drop してから平文 3 列を物理 DROP する。
    with op.batch_alter_table("journal_entry_lines") as b:
        b.drop_constraint("fk_jel_account", type_="foreignkey")
        b.drop_column("account_code")
        b.drop_column("debit_amount")
        b.drop_column("credit_amount")


def downgrade():
    # 平文 3 列を nullable で復元 (平文データは E2EE 移行で失われているため値は
    # 復元されない)。複合 FK fk_jel_account は account_code が NULL/値なしのため
    # 再作成しない (整合性検査はクライアント + 監査時検査の責務)。
    with op.batch_alter_table("journal_entry_lines") as b:
        b.add_column(sa.Column("account_code", sa.String(10), nullable=True))
        b.add_column(sa.Column("debit_amount", sa.Numeric(12, 0), nullable=True))
        b.add_column(sa.Column("credit_amount", sa.Numeric(12, 0), nullable=True))
