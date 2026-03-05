"""科目マッピングを複数決算書種別で共用可能にする

元入金・事業主借貸などのB/S科目は一般用・不動産用の両方にマッピングされ得る。
ユニーク制約を (user_id, account_code) → (user_id, account_code, field_id) に変更。

Revision ID: 031_tax_mapping_multi_form
Revises: 030_tax_form_fields
"""
from alembic import op

revision = "031_tax_mapping_multi_form"
down_revision = "030_tax_form_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("uq_tax_mapping_user_account", "tax_form_mappings", type_="unique")
    op.create_unique_constraint(
        "uq_tax_mapping_user_account_field",
        "tax_form_mappings",
        ["user_id", "account_code", "field_id"],
    )


def downgrade():
    op.drop_constraint("uq_tax_mapping_user_account_field", "tax_form_mappings", type_="unique")
    op.create_unique_constraint(
        "uq_tax_mapping_user_account",
        "tax_form_mappings",
        ["user_id", "account_code"],
    )
