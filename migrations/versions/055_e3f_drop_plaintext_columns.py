"""E3-F: 平文列 DROP + encrypted_blob/blob_iv NOT NULL 化 (#220)

dual-read 撤去 (#220) の最終。D-6-4 で平文 WRITE を停止し、pre-1/pre-2 で平文
READ を一掃したため、journal_entries / journal_entry_lines / medical_expenses の
平文列を物理 DROP する。本体は encrypted_blob のみ。closing (損益振替・自動生成)
仕訳は空 blob (b"") + ゼロ IV のセンチネルを持つため encrypted_blob / blob_iv は
NOT NULL 化できる。fiscal_year / fiscal_month は nullable のまま据置 (実運用では
常に populate 済だが、本マイグレでは制約変更しない)。

DROP する平文列:
- journal_entries: date / description / source / fiscal_period
- journal_entry_lines: description
- medical_expenses: date / patient_name / hospital_name / treatment_description /
  provider_type / amount_paid / insurance_reimbursement

date 列の専用 index は存在しないため撤去対象なし (batch_id / fiscal_year の
index のみ存在し、いずれも維持)。

downgrade は平文列を nullable で復元するのみ (平文データは E2EE 移行で失われて
いるため値は復元されない = 実質片道のマイグレーション)。

Revision ID: 055_e3f_drop_plaintext_columns
Revises: 054_e3f_add_closing_month
Create Date: 2026-05-31
"""

from alembic import op
import sqlalchemy as sa


revision = "055_e3f_drop_plaintext_columns"
down_revision = "054_e3f_add_closing_month"
branch_labels = None
depends_on = None

_BLOB_TABLES = ("journal_entries", "journal_entry_lines", "medical_expenses")


def _count(conn, sql):
    return conn.execute(sa.text(sql)).scalar() or 0


def upgrade():
    # === ガード (#338 / E7): 未暗号化平文の破壊防止 ===
    # 本マイグレは平文列を物理 DROP する。v4.0.0 など E2EE データ移行 (平文→
    # encrypted_blob 暗号化) が未完了の DB に対してそのまま走ると、平文を暗号化
    # しないまま DROP して仕訳ヘッダ・明細・医療費の内容を恒久的に失う。よって
    # 「暗号文 (encrypted_blob) が空/NULL なのに平文が残っている行」を検出したら
    # 中断する。E7 のデータ暗号化を先に完了させてから再実行すること。
    # closing 空 blob センチネル (平文も空) や空テーブル・E2EE 済み DB では発火しない。
    conn = op.get_bind()
    unenc_je = _count(
        conn,
        "SELECT COUNT(*) FROM journal_entries "
        "WHERE (encrypted_blob IS NULL OR octet_length(encrypted_blob)=0) AND ("
        "  date IS NOT NULL"
        "  OR (description IS NOT NULL AND description<>'')"
        "  OR (source IS NOT NULL AND source<>'journal')"
        "  OR fiscal_period IS NOT NULL)",
    )
    unenc_jel = _count(
        conn,
        "SELECT COUNT(*) FROM journal_entry_lines "
        "WHERE (encrypted_blob IS NULL OR octet_length(encrypted_blob)=0) AND ("
        "  (description IS NOT NULL AND description<>'')"
        "  OR account_code IS NOT NULL"
        "  OR debit_amount IS NOT NULL"
        "  OR credit_amount IS NOT NULL)",
    )
    unenc_me = _count(
        conn,
        "SELECT COUNT(*) FROM medical_expenses "
        "WHERE (encrypted_blob IS NULL OR octet_length(encrypted_blob)=0) AND ("
        "  date IS NOT NULL"
        "  OR (patient_name IS NOT NULL AND patient_name<>'')"
        "  OR (hospital_name IS NOT NULL AND hospital_name<>'')"
        "  OR (treatment_description IS NOT NULL AND treatment_description<>'')"
        "  OR (provider_type IS NOT NULL AND provider_type<>'')"
        "  OR amount_paid IS NOT NULL"
        "  OR insurance_reimbursement<>0)",
    )
    if unenc_je or unenc_jel or unenc_me:
        raise RuntimeError(
            "E2EE データ移行が未完了です。平文が残っているのに encrypted_blob が空の行を"
            f" 検出しました (journal_entries={unenc_je}, journal_entry_lines={unenc_jel},"
            f" medical_expenses={unenc_me})。平文列を DROP する前に E7 のデータ暗号化"
            " (temp-MK によるサーバ側一括暗号化、または各利用者クライアントでの暗号化) を"
            " 完了させてください。未暗号化のまま DROP すると内容が恒久的に失われます。"
        )

    # NOT NULL 化の前に、万一 NULL の暗号文があれば空 blob センチネルで埋める
    # (ガード通過後なので平文の無い退化行のみが対象。E2EE 移行済 DB では発生しない)。
    for t in _BLOB_TABLES:
        op.execute(
            sa.text(
                f"UPDATE {t} SET encrypted_blob = :v WHERE encrypted_blob IS NULL"
            ).bindparams(v=b"")
        )
        op.execute(
            sa.text(
                f"UPDATE {t} SET blob_iv = :v WHERE blob_iv IS NULL"
            ).bindparams(v=b"")
        )

    with op.batch_alter_table("journal_entries") as b:
        b.alter_column(
            "encrypted_blob", existing_type=sa.LargeBinary(), nullable=False
        )
        b.alter_column(
            "blob_iv", existing_type=sa.LargeBinary(), nullable=False
        )
        b.drop_column("date")
        b.drop_column("description")
        b.drop_column("source")
        b.drop_column("fiscal_period")

    with op.batch_alter_table("journal_entry_lines") as b:
        b.alter_column(
            "encrypted_blob", existing_type=sa.LargeBinary(), nullable=False
        )
        b.alter_column(
            "blob_iv", existing_type=sa.LargeBinary(), nullable=False
        )
        b.drop_column("description")

    with op.batch_alter_table("medical_expenses") as b:
        b.alter_column(
            "encrypted_blob", existing_type=sa.LargeBinary(), nullable=False
        )
        b.alter_column(
            "blob_iv", existing_type=sa.LargeBinary(), nullable=False
        )
        b.drop_column("date")
        b.drop_column("patient_name")
        b.drop_column("hospital_name")
        b.drop_column("treatment_description")
        b.drop_column("provider_type")
        b.drop_column("amount_paid")
        b.drop_column("insurance_reimbursement")


def downgrade():
    # 平文列を nullable で復元 (平文データは失われているため NULL/default のまま)。
    with op.batch_alter_table("journal_entries") as b:
        b.alter_column(
            "encrypted_blob", existing_type=sa.LargeBinary(), nullable=True
        )
        b.alter_column(
            "blob_iv", existing_type=sa.LargeBinary(), nullable=True
        )
        b.add_column(sa.Column("date", sa.Date(), nullable=True))
        b.add_column(
            sa.Column(
                "description", sa.String(255), nullable=False, server_default=""
            )
        )
        b.add_column(
            sa.Column(
                "source", sa.String(20), nullable=False, server_default="journal"
            )
        )
        b.add_column(sa.Column("fiscal_period", sa.Integer(), nullable=True))

    with op.batch_alter_table("journal_entry_lines") as b:
        b.alter_column(
            "encrypted_blob", existing_type=sa.LargeBinary(), nullable=True
        )
        b.alter_column(
            "blob_iv", existing_type=sa.LargeBinary(), nullable=True
        )
        b.add_column(
            sa.Column("description", sa.String(255), server_default="")
        )

    with op.batch_alter_table("medical_expenses") as b:
        b.alter_column(
            "encrypted_blob", existing_type=sa.LargeBinary(), nullable=True
        )
        b.alter_column(
            "blob_iv", existing_type=sa.LargeBinary(), nullable=True
        )
        b.add_column(sa.Column("date", sa.Date(), nullable=True))
        b.add_column(
            sa.Column(
                "patient_name", sa.String(100), nullable=False, server_default=""
            )
        )
        b.add_column(
            sa.Column(
                "hospital_name", sa.String(200), nullable=False, server_default=""
            )
        )
        b.add_column(
            sa.Column(
                "treatment_description",
                sa.String(255),
                nullable=False,
                server_default="",
            )
        )
        b.add_column(sa.Column("provider_type", sa.String(20), nullable=True))
        b.add_column(sa.Column("amount_paid", sa.Integer(), nullable=True))
        b.add_column(
            sa.Column(
                "insurance_reimbursement",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
