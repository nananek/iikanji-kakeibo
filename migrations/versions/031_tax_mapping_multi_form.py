"""科目マッピングを複数決算書種別対応にする

save_mappings/set_mappingをform_typeスコープに変更するが、
1科目は1つのform_typeにのみマッピング可能なため、
ユニーク制約は (user_id, account_code) のまま維持する。

Revision ID: 031_tax_mapping_multi_form
Revises: 030_tax_form_fields
"""

revision = "031_tax_mapping_multi_form"
down_revision = "030_tax_form_fields"
branch_labels = None
depends_on = None


def upgrade():
    # アプリロジックのみの変更。DB制約は変更不要。
    pass


def downgrade():
    pass
