"""標準勘定科目の初期データ投入"""

from app.extensions import db
from app.models.account import AccountType, Account


ACCOUNT_TYPES = [
    {"name": "資産", "code": "asset", "normal_balance": "debit", "display_order": 1},
    {"name": "負債", "code": "liability", "normal_balance": "credit", "display_order": 2},
    {"name": "純資産", "code": "equity", "normal_balance": "credit", "display_order": 3},
    {"name": "収益", "code": "revenue", "normal_balance": "credit", "display_order": 4},
    {"name": "費用", "code": "expense", "normal_balance": "debit", "display_order": 5},
]

# (code, name, account_type_code, tax_category, description)
STANDARD_ACCOUNTS = [
    # 資産
    ("1010", "現金", "asset", None, "手元の現金"),
    ("1020", "普通預金", "asset", None, "銀行普通預金"),
    ("1030", "定期預金", "asset", None, "銀行定期預金"),
    ("1040", "電子マネー", "asset", None, "Suica・PayPay等"),
    ("1050", "有価証券", "asset", None, "株式・投資信託等"),
    ("1060", "未収入金", "asset", None, "未回収の収入"),
    # 負債
    ("2010", "クレジットカード", "liability", None, "クレジットカード未払金"),
    ("2020", "未払金", "liability", None, "その他未払金"),
    ("2030", "借入金", "liability", None, "各種ローン"),
    ("2040", "住宅ローン", "liability", None, "住宅ローン残高"),
    # 純資産
    ("3010", "元入金", "equity", None, "開始時の資本"),
    ("3020", "繰越利益", "equity", None, "前年度からの繰越"),
    ("3030", "事業主", "equity", None, "監査用: 非公開科目の代替"),
    # 収益
    ("4010", "給与収入", "revenue", None, "給与・賞与"),
    ("4020", "事業収入", "revenue", None, "副業・フリーランス収入"),
    ("4030", "利息収入", "revenue", None, "預金利息"),
    ("4040", "配当収入", "revenue", None, "株式配当"),
    ("4050", "雑収入", "revenue", None, "その他の収入"),
    # 費用 — 一般生活費
    ("5010", "食費", "expense", None, "食料品・外食"),
    ("5020", "住居費", "expense", None, "家賃・管理費"),
    ("5030", "水道光熱費", "expense", None, "水道・ガス・電気"),
    ("5040", "通信費", "expense", None, "携帯・インターネット"),
    ("5050", "交通費", "expense", None, "電車・バス・ガソリン"),
    ("5060", "日用品費", "expense", None, "消耗品・日用雑貨"),
    ("5070", "被服費", "expense", None, "衣服・靴"),
    ("5080", "美容費", "expense", None, "美容院・化粧品"),
    ("5090", "交際費", "expense", None, "冠婚葬祭・贈答"),
    ("5100", "趣味・娯楽費", "expense", None, "レジャー・書籍"),
    ("5110", "教育費", "expense", None, "学費・習い事"),
    ("5120", "雑費", "expense", None, "その他の費用"),
    # 費用 — 医療費
    ("6010", "医療費", "expense", "medical", "病院・薬局"),
    # 費用 — 社会保険料
    ("6020", "健康保険料", "expense", "social_insurance", "健康保険"),
    ("6030", "厚生年金保険料", "expense", "social_insurance", "厚生年金"),
    ("6040", "国民年金保険料", "expense", "social_insurance", "国民年金"),
    ("6050", "国民健康保険料", "expense", "social_insurance", "国保"),
    ("6060", "雇用保険料", "expense", "social_insurance", "雇用保険"),
    ("6070", "介護保険料", "expense", "social_insurance", "介護保険"),
    # 費用 — 保険・控除
    ("7010", "生命保険料", "expense", "life_insurance", "生命保険"),
    ("7020", "個人年金保険料", "expense", "life_insurance", "個人年金"),
    ("7030", "地震保険料", "expense", "earthquake_insurance", "地震保険"),
    ("7040", "ふるさと納税", "expense", "donation", "ふるさと納税"),
    ("7050", "その他寄附金", "expense", "donation", "寄附金"),
    ("7060", "iDeCo掛金", "expense", "ideco", "個人型確定拠出年金"),
    ("7070", "小規模企業共済", "expense", "ideco", "小規模企業共済"),
    # 費用 — 税金
    ("8010", "源泉所得税", "expense", "withholding_tax", "給与天引き所得税"),
    ("8020", "住民税", "expense", "resident_tax", "給与天引き住民税"),
]


def seed_account_types():
    """勘定科目区分を投入（全ユーザー共通）"""
    for data in ACCOUNT_TYPES:
        existing = AccountType.query.filter_by(code=data["code"]).first()
        if not existing:
            db.session.add(AccountType(**data))
    db.session.commit()


def seed_accounts_for_user(user_id):
    """指定ユーザーに標準勘定科目を投入"""
    type_map = {at.code: at.id for at in AccountType.query.all()}
    existing_codes = {
        a.code
        for a in Account.query.filter_by(user_id=user_id).all()
    }

    order = 0
    for code, name, type_code, tax_cat, desc in STANDARD_ACCOUNTS:
        if code in existing_codes:
            continue
        order += 10
        account = Account(
            user_id=user_id,
            account_type_id=type_map[type_code],
            code=code,
            name=name,
            description=desc,
            tax_category=tax_cat,
            is_system=True,
            is_active=True,
            display_order=order,
        )
        db.session.add(account)

    db.session.commit()
