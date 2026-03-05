"""青色申告決算書の欄定義とマッピング

Revision ID: 030_tax_form_fields
Revises: 029_csv_column_profiles
"""
from alembic import op
import sqlalchemy as sa

revision = "030_tax_form_fields"
down_revision = "029_csv_column_profiles"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tax_form_fields",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("form_type", sa.String(20), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(30), nullable=False),
        sa.Column("row_code", sa.String(10), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("account_type_code", sa.String(10), nullable=False),
        sa.Column("suggested_code", sa.String(10), nullable=True),
        sa.Column("is_subtotal", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_user_defined", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "tax_form_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_code", sa.String(10), nullable=False),
        sa.Column("field_id", sa.Integer(), sa.ForeignKey("tax_form_fields.id"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "account_code"],
            ["accounts.user_id", "accounts.code"],
            name="fk_tax_mapping_account",
        ),
        sa.UniqueConstraint(
            "user_id", "account_code",
            name="uq_tax_mapping_user_account",
        ),
    )

    # 青色申告決算書（一般用）のマスタデータを投入
    tax_form_fields = sa.table(
        "tax_form_fields",
        sa.column("form_type", sa.String),
        sa.column("page", sa.Integer),
        sa.column("section", sa.String),
        sa.column("row_code", sa.String),
        sa.column("name", sa.String),
        sa.column("account_type_code", sa.String),
        sa.column("suggested_code", sa.String),
        sa.column("is_subtotal", sa.Boolean),
        sa.column("is_user_defined", sa.Boolean),
        sa.column("display_order", sa.Integer),
    )

    fields = _general_form_fields()
    op.bulk_insert(tax_form_fields, fields)


def downgrade():
    op.drop_table("tax_form_mappings")
    op.drop_table("tax_form_fields")


def _general_form_fields():
    """青色申告決算書（一般用）全欄定義"""
    F = "general"
    fields = []
    order = 0

    def add(page, section, row_code, name, type_code, suggested=None,
            subtotal=False, user_defined=False):
        nonlocal order
        order += 10
        fields.append({
            "form_type": F,
            "page": page,
            "section": section,
            "row_code": row_code,
            "name": name,
            "account_type_code": type_code,
            "suggested_code": suggested,
            "is_subtotal": subtotal,
            "is_user_defined": user_defined,
            "display_order": order,
        })

    # --- P1: 損益計算書 ---
    # 売上
    add(1, "revenue", "1", "売上（収入）金額", "revenue", "9010")

    # 売上原価
    add(1, "cost_of_sales", "2", "期首商品棚卸高", "asset", "9110")
    add(1, "cost_of_sales", "3", "仕入金額", "expense", "9120")
    add(1, "cost_of_sales", "4", "小計", "expense", subtotal=True)
    add(1, "cost_of_sales", "5", "期末商品棚卸高", "asset", "9130")
    add(1, "cost_of_sales", "6", "差引原価", "expense", subtotal=True)
    add(1, "cost_of_sales", "7", "差引金額", "revenue", subtotal=True)

    # 経費
    add(1, "expenses", "8", "租税公課", "expense", "9210")
    add(1, "expenses", "9", "荷造運賃", "expense", "9220")
    add(1, "expenses", "10", "水道光熱費", "expense", "9230")
    add(1, "expenses", "11", "旅費交通費", "expense", "9240")
    add(1, "expenses", "12", "通信費", "expense", "9250")
    add(1, "expenses", "13", "広告宣伝費", "expense", "9260")
    add(1, "expenses", "14", "接待交際費", "expense", "9270")
    add(1, "expenses", "15", "損害保険料", "expense", "9280")
    add(1, "expenses", "16", "修繕費", "expense", "9290")
    add(1, "expenses", "17", "消耗品費", "expense", "9300")
    add(1, "expenses", "18", "減価償却費", "expense", "9310")
    add(1, "expenses", "19", "福利厚生費", "expense", "9320")
    add(1, "expenses", "20", "給料賃金", "expense", "9330")
    add(1, "expenses", "21", "外注工賃", "expense", "9340")
    add(1, "expenses", "22", "利子割引料", "expense", "9350")
    add(1, "expenses", "23", "地代家賃", "expense", "9360")
    add(1, "expenses", "24", "貸倒金", "expense", "9370")
    add(1, "expenses", "25", "雑費", "expense", "9380")
    # ユーザー自由記入欄（空欄×4）
    add(1, "expenses", "26", "空欄1", "expense", "9390", user_defined=True)
    add(1, "expenses", "27", "空欄2", "expense", "9400", user_defined=True)
    add(1, "expenses", "28", "空欄3", "expense", "9410", user_defined=True)
    add(1, "expenses", "29", "空欄4", "expense", "9420", user_defined=True)
    add(1, "expenses", "30", "経費計", "expense", subtotal=True)

    # 所得金額
    add(1, "income", "31", "差引金額", "revenue", subtotal=True)
    add(1, "income", "32", "専従者給与", "expense", "9430")
    add(1, "income", "33", "各種引当金・準備金等繰戻額", "revenue")
    add(1, "income", "34", "各種引当金・準備金等繰入額", "expense")
    add(1, "income", "35", "青色申告特別控除前の所得金額", "revenue", subtotal=True)
    add(1, "income", "36", "青色申告特別控除額", "expense", subtotal=True)
    add(1, "income", "37", "所得金額", "revenue", subtotal=True)

    # --- P4: 貸借対照表 ---
    # 資産の部
    add(4, "bs_assets", "A1", "現金", "asset", "9510")
    add(4, "bs_assets", "A2", "当座預金", "asset", "9520")
    add(4, "bs_assets", "A3", "定期預金", "asset", "9530")
    add(4, "bs_assets", "A4", "その他の預金", "asset", "9540")
    add(4, "bs_assets", "A5", "受取手形", "asset", "9550")
    add(4, "bs_assets", "A6", "売掛金", "asset", "9560")
    add(4, "bs_assets", "A7", "有価証券", "asset", "9570")
    add(4, "bs_assets", "A8", "棚卸資産", "asset", "9580")
    add(4, "bs_assets", "A9", "前払金", "asset", "9590")
    add(4, "bs_assets", "A10", "貸付金", "asset", "9600")
    add(4, "bs_assets", "A11", "建物", "asset", "9610")
    add(4, "bs_assets", "A12", "建物附属設備", "asset", "9620")
    add(4, "bs_assets", "A13", "機械装置", "asset", "9630")
    add(4, "bs_assets", "A14", "車両運搬具", "asset", "9640")
    add(4, "bs_assets", "A15", "工具器具備品", "asset", "9650")
    add(4, "bs_assets", "A16", "土地", "asset", "9660")
    add(4, "bs_assets", "A17", "事業主貸", "asset", "9670")
    add(4, "bs_assets", "AT", "資産合計", "asset", subtotal=True)

    # 負債・資本の部
    add(4, "bs_liabilities", "L1", "支払手形", "liability", "9710")
    add(4, "bs_liabilities", "L2", "買掛金", "liability", "9720")
    add(4, "bs_liabilities", "L3", "借入金", "liability", "9730")
    add(4, "bs_liabilities", "L4", "未払金", "liability", "9740")
    add(4, "bs_liabilities", "L5", "前受金", "liability", "9750")
    add(4, "bs_liabilities", "L6", "預り金", "liability", "9760")
    add(4, "bs_liabilities", "L7", "貸倒引当金", "liability", "9770")
    add(4, "bs_liabilities", "L8", "空欄", "liability", user_defined=True)
    add(4, "bs_liabilities", "L9", "事業主借", "liability", "9780")
    add(4, "bs_liabilities", "L10", "元入金", "equity", "9790")
    add(4, "bs_liabilities", "L11", "青色申告特別控除前の所得金額", "equity", subtotal=True)
    add(4, "bs_liabilities", "LT", "負債・資本合計", "liability", subtotal=True)

    return fields
