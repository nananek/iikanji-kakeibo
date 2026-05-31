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


def upgrade():
    # NOT NULL 化の前に、万一 NULL の暗号文があれば空 blob センチネルで埋める
    # (E2EE 移行済 DB では発生しない安全網)。
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
