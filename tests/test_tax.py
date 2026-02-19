"""tax.py のテスト — 確定申告集計・医療費集計・月次比較・着地予想・収支サマリー"""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.account import Account, AccountType
from app.models.medical import MedicalExpense
from app.services.tax import (
    get_income_expense_summary,
    get_medical_summary,
    get_month_projection,
    get_monthly_comparison,
    get_tax_summary,
)
from tests.conftest import make_journal


# --- 税務科目フィクスチャ ---


@pytest.fixture
def tax_accounts(db, user, account_types):
    """税務関連の科目セット（生命保険・社保・地震・寄附・iDeCo・源泉・住民・医療）"""
    expense = account_types["expense"]
    accts = [
        Account(user_id=user.id, account_type_id=expense.id,
                code="6010", name="医療費", tax_category="medical",
                is_active=True, display_order=200),
        Account(user_id=user.id, account_type_id=expense.id,
                code="6020", name="健康保険料", tax_category="social_insurance",
                is_active=True, display_order=201),
        Account(user_id=user.id, account_type_id=expense.id,
                code="6030", name="厚生年金保険料", tax_category="social_insurance",
                is_active=True, display_order=202),
        Account(user_id=user.id, account_type_id=expense.id,
                code="6040", name="国民年金保険料", tax_category="social_insurance",
                is_active=True, display_order=203),
        Account(user_id=user.id, account_type_id=expense.id,
                code="7010", name="生命保険料", tax_category="life_insurance",
                is_active=True, display_order=210),
        Account(user_id=user.id, account_type_id=expense.id,
                code="7020", name="個人年金保険料", tax_category="life_insurance",
                is_active=True, display_order=211),
        Account(user_id=user.id, account_type_id=expense.id,
                code="7030", name="地震保険料", tax_category="earthquake_insurance",
                is_active=True, display_order=212),
        Account(user_id=user.id, account_type_id=expense.id,
                code="7040", name="ふるさと納税", tax_category="donation",
                is_active=True, display_order=213),
        Account(user_id=user.id, account_type_id=expense.id,
                code="7060", name="iDeCo掛金", tax_category="ideco",
                is_active=True, display_order=214),
        Account(user_id=user.id, account_type_id=expense.id,
                code="8010", name="源泉所得税", tax_category="withholding_tax",
                is_active=True, display_order=220),
        Account(user_id=user.id, account_type_id=expense.id,
                code="8020", name="住民税", tax_category="resident_tax",
                is_active=True, display_order=221),
    ]
    db.session.add_all(accts)
    db.session.commit()
    return {a.code: a for a in accts}


# =================================================================
# get_tax_summary
# =================================================================


class TestGetTaxSummary:
    def test_empty(self, db, user, accounts):
        """控除対象の仕訳がない場合は空"""
        summary = get_tax_summary(user.id, 2026)
        assert summary == {}

    def test_life_insurance(self, db, user, accounts, tax_accounts):
        """生命保険料控除が集計される"""
        make_journal(db, user.id, tax_accounts["7010"].id, accounts["1010"].id,
                     88370, entry_date=date(2026, 12, 24),
                     description="第一生命保険株式会社")
        summary = get_tax_summary(user.id, 2026)
        assert "life_insurance" in summary
        assert summary["life_insurance"]["total"] == 88370
        assert summary["life_insurance"]["label"] == "生命保険料控除"

    def test_life_insurance_multiple_accounts(self, db, user, accounts, tax_accounts):
        """生命保険と個人年金が同じカテゴリで合算される"""
        make_journal(db, user.id, tax_accounts["7010"].id, accounts["1010"].id,
                     50000, entry_date=date(2026, 6, 15))
        make_journal(db, user.id, tax_accounts["7020"].id, accounts["1010"].id,
                     30000, entry_date=date(2026, 6, 15))
        summary = get_tax_summary(user.id, 2026)
        assert summary["life_insurance"]["total"] == 80000
        assert len(summary["life_insurance"]["accounts"]) == 2

    def test_social_insurance(self, db, user, accounts, tax_accounts):
        """社会保険料控除の集計（複数科目）"""
        make_journal(db, user.id, tax_accounts["6020"].id, accounts["1020"].id,
                     50000, entry_date=date(2026, 6, 25))
        make_journal(db, user.id, tax_accounts["6030"].id, accounts["1020"].id,
                     90000, entry_date=date(2026, 6, 25))
        make_journal(db, user.id, tax_accounts["6040"].id, accounts["1020"].id,
                     16590, entry_date=date(2026, 6, 25))
        summary = get_tax_summary(user.id, 2026)
        assert "social_insurance" in summary
        assert summary["social_insurance"]["total"] == 156590
        assert len(summary["social_insurance"]["accounts"]) == 3

    def test_earthquake_insurance(self, db, user, accounts, tax_accounts):
        """地震保険料控除"""
        make_journal(db, user.id, tax_accounts["7030"].id, accounts["1020"].id,
                     12000, entry_date=date(2026, 3, 15))
        summary = get_tax_summary(user.id, 2026)
        assert "earthquake_insurance" in summary
        assert summary["earthquake_insurance"]["total"] == 12000

    def test_donation(self, db, user, accounts, tax_accounts):
        """寄附金控除"""
        make_journal(db, user.id, tax_accounts["7040"].id, accounts["1020"].id,
                     50000, entry_date=date(2026, 11, 30))
        summary = get_tax_summary(user.id, 2026)
        assert "donation" in summary
        assert summary["donation"]["total"] == 50000

    def test_ideco(self, db, user, accounts, tax_accounts):
        """小規模企業共済等掛金控除"""
        for m in range(1, 13):
            make_journal(db, user.id, tax_accounts["7060"].id, accounts["1020"].id,
                         23000, entry_date=date(2026, m, 26))
        summary = get_tax_summary(user.id, 2026)
        assert "ideco" in summary
        assert summary["ideco"]["total"] == 276000

    def test_withholding_tax(self, db, user, accounts, tax_accounts):
        """源泉所得税が集計される"""
        make_journal(db, user.id, tax_accounts["8010"].id, accounts["1020"].id,
                     5000, entry_date=date(2026, 1, 25))
        summary = get_tax_summary(user.id, 2026)
        assert "withholding_tax" in summary
        assert summary["withholding_tax"]["total"] == 5000

    def test_closing_entries_excluded(self, db, user, accounts, tax_accounts):
        """損益振替仕訳（source=closing）は集計から除外される"""
        make_journal(db, user.id, tax_accounts["7010"].id, accounts["1010"].id,
                     88370, entry_date=date(2026, 12, 24))
        # 損益振替（逆仕訳）
        make_journal(db, user.id, accounts["3010"].id, tax_accounts["7010"].id,
                     88370, entry_date=date(2026, 12, 31),
                     source="closing", fiscal_period=16)
        summary = get_tax_summary(user.id, 2026)
        assert summary["life_insurance"]["total"] == 88370

    def test_medical_excluded(self, db, user, accounts, tax_accounts):
        """医療費カテゴリは集計から除外される（専用セクションで表示）"""
        make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                     5000, entry_date=date(2026, 3, 15))
        summary = get_tax_summary(user.id, 2026)
        assert "medical" not in summary

    def test_resident_tax_excluded(self, db, user, accounts, tax_accounts):
        """住民税は集計から除外される（確定申告と無関係）"""
        make_journal(db, user.id, tax_accounts["8020"].id, accounts["1020"].id,
                     11900, entry_date=date(2026, 6, 25))
        summary = get_tax_summary(user.id, 2026)
        assert "resident_tax" not in summary

    def test_year_boundary(self, db, user, accounts, tax_accounts):
        """年度をまたぐ仕訳は対象年のみ集計される"""
        make_journal(db, user.id, tax_accounts["7010"].id, accounts["1010"].id,
                     50000, entry_date=date(2025, 12, 31))
        make_journal(db, user.id, tax_accounts["7010"].id, accounts["1010"].id,
                     60000, entry_date=date(2026, 1, 1))
        make_journal(db, user.id, tax_accounts["7010"].id, accounts["1010"].id,
                     70000, entry_date=date(2026, 12, 31))
        make_journal(db, user.id, tax_accounts["7010"].id, accounts["1010"].id,
                     80000, entry_date=date(2027, 1, 1))
        summary = get_tax_summary(user.id, 2026)
        assert summary["life_insurance"]["total"] == 130000  # 60000 + 70000

    def test_no_tax_category_excluded(self, db, user, accounts):
        """tax_category が NULL の科目は集計されない"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     3000, entry_date=date(2026, 2, 15))
        summary = get_tax_summary(user.id, 2026)
        assert summary == {}

    def test_csv_source_included(self, db, user, accounts, tax_accounts):
        """CSV取込（source=csv）の仕訳も集計される"""
        make_journal(db, user.id, tax_accounts["6020"].id, accounts["1020"].id,
                     45000, entry_date=date(2026, 5, 25), source="csv")
        summary = get_tax_summary(user.id, 2026)
        assert summary["social_insurance"]["total"] == 45000

    def test_multiple_categories(self, db, user, accounts, tax_accounts):
        """複数カテゴリが同時に集計される"""
        make_journal(db, user.id, tax_accounts["7010"].id, accounts["1010"].id,
                     80000, entry_date=date(2026, 12, 24))
        make_journal(db, user.id, tax_accounts["6020"].id, accounts["1020"].id,
                     50000, entry_date=date(2026, 6, 25))
        make_journal(db, user.id, tax_accounts["7040"].id, accounts["1020"].id,
                     30000, entry_date=date(2026, 11, 30))
        summary = get_tax_summary(user.id, 2026)
        assert len(summary) == 3
        assert "life_insurance" in summary
        assert "social_insurance" in summary
        assert "donation" in summary

    def test_user_isolation(self, db, user, accounts, tax_accounts, auditor):
        """他ユーザーのデータは集計されない"""
        make_journal(db, user.id, tax_accounts["7010"].id, accounts["1010"].id,
                     80000, entry_date=date(2026, 12, 24))
        summary = get_tax_summary(auditor.id, 2026)
        assert summary == {}


# =================================================================
# get_medical_summary
# =================================================================


class TestGetMedicalSummary:
    def test_empty(self, db, user, accounts, tax_accounts):
        """医療費仕訳がない場合"""
        result = get_medical_summary(user.id, 2026)
        assert result["expenses"] == []
        assert result["total_paid"] == 0
        assert result["total_reimbursed"] == 0
        assert result["net_total"] == 0
        assert result["by_patient"] == []

    def test_basic_medical_expense(self, db, user, accounts, tax_accounts):
        """基本的な医療費集計"""
        make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                     5000, entry_date=date(2026, 3, 15), description="内科受診")
        result = get_medical_summary(user.id, 2026)
        assert result["total_paid"] == 5000
        assert result["total_reimbursed"] == 0
        assert result["net_total"] == 5000
        assert len(result["expenses"]) == 1
        assert result["expenses"][0]["description"] == "内科受診"

    def test_with_medical_expense_detail(self, db, user, accounts, tax_accounts):
        """MedicalExpense レコード付きの集計"""
        entry = make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                             8000, entry_date=date(2026, 4, 10), description="歯科治療")
        me = MedicalExpense(
            user_id=user.id, journal_entry_id=entry.id,
            date=date(2026, 4, 10),
            patient_name="山田太郎", hospital_name="山田歯科",
            treatment_description="虫歯治療",
            provider_type="hospital",
            amount_paid=8000, insurance_reimbursement=2000,
        )
        db.session.add(me)
        db.session.commit()

        result = get_medical_summary(user.id, 2026)
        assert result["total_paid"] == 8000
        assert result["total_reimbursed"] == 2000
        assert result["net_total"] == 6000
        assert result["expenses"][0]["patient_name"] == "山田太郎"
        assert result["expenses"][0]["hospital_name"] == "山田歯科"
        assert result["expenses"][0]["provider_type"] == "hospital"

    def test_by_patient_aggregation(self, db, user, accounts, tax_accounts):
        """受診者別・医療機関別の階層集計"""
        # 山田太郎: A病院 2回、B薬局 1回
        e1 = make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                          3000, entry_date=date(2026, 2, 10))
        db.session.add(MedicalExpense(
            user_id=user.id, journal_entry_id=e1.id, date=date(2026, 2, 10),
            patient_name="山田太郎", hospital_name="A病院", provider_type="hospital",
            amount_paid=3000, insurance_reimbursement=0,
        ))
        e2 = make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                          5000, entry_date=date(2026, 3, 20))
        db.session.add(MedicalExpense(
            user_id=user.id, journal_entry_id=e2.id, date=date(2026, 3, 20),
            patient_name="山田太郎", hospital_name="A病院", provider_type="hospital",
            amount_paid=5000, insurance_reimbursement=1000,
        ))
        e3 = make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                          1500, entry_date=date(2026, 3, 20))
        db.session.add(MedicalExpense(
            user_id=user.id, journal_entry_id=e3.id, date=date(2026, 3, 20),
            patient_name="山田太郎", hospital_name="B薬局", provider_type="pharmacy",
            amount_paid=1500, insurance_reimbursement=0,
        ))
        # 山田花子: C病院 1回
        e4 = make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                          2000, entry_date=date(2026, 5, 1))
        db.session.add(MedicalExpense(
            user_id=user.id, journal_entry_id=e4.id, date=date(2026, 5, 1),
            patient_name="山田花子", hospital_name="C病院", provider_type="hospital",
            amount_paid=2000, insurance_reimbursement=500,
        ))
        db.session.commit()

        result = get_medical_summary(user.id, 2026)
        assert result["total_paid"] == 11500
        assert result["total_reimbursed"] == 1500
        assert result["net_total"] == 10000

        # by_patient は支払額降順
        assert len(result["by_patient"]) == 2
        taro = result["by_patient"][0]  # 太郎: 9500
        assert taro["name"] == "山田太郎"
        assert taro["paid"] == 9500
        assert taro["reimbursed"] == 1000
        assert len(taro["hospitals"]) == 2

        hanako = result["by_patient"][1]  # 花子: 2000
        assert hanako["name"] == "山田花子"
        assert hanako["paid"] == 2000

    def test_without_detail_uses_defaults(self, db, user, accounts, tax_accounts):
        """MedicalExpense レコードがない場合はデフォルト値"""
        make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                     3000, entry_date=date(2026, 6, 15))
        result = get_medical_summary(user.id, 2026)
        e = result["expenses"][0]
        assert e["patient_name"] == ""
        assert e["hospital_name"] == ""
        assert e["insurance_reimbursement"] == 0

    def test_by_patient_unset_uses_placeholder(self, db, user, accounts, tax_accounts):
        """受診者名未設定は「(未設定)」でグルーピング"""
        make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                     3000, entry_date=date(2026, 6, 15))
        result = get_medical_summary(user.id, 2026)
        assert result["by_patient"][0]["name"] == "(未設定)"

    def test_year_boundary(self, db, user, accounts, tax_accounts):
        """年度境界: 対象年のみ集計"""
        make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                     3000, entry_date=date(2025, 12, 31))
        make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                     5000, entry_date=date(2026, 1, 1))
        make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                     7000, entry_date=date(2026, 12, 31))
        result = get_medical_summary(user.id, 2026)
        assert result["total_paid"] == 12000  # 5000 + 7000

    def test_closing_entry_not_counted(self, db, user, accounts, tax_accounts):
        """損益振替仕訳は debit_amount=0 なので集計されない"""
        make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                     5000, entry_date=date(2026, 3, 15))
        # 損益振替: 医療費科目が貸方(credit)に来る → debit_amount=0 なので除外される
        make_journal(db, user.id, accounts["3010"].id, tax_accounts["6010"].id,
                     5000, entry_date=date(2026, 12, 31),
                     source="closing", fiscal_period=16)
        result = get_medical_summary(user.id, 2026)
        assert result["total_paid"] == 5000

    def test_user_isolation(self, db, user, accounts, tax_accounts, auditor):
        """他ユーザーの医療費は集計されない"""
        make_journal(db, user.id, tax_accounts["6010"].id, accounts["1010"].id,
                     5000, entry_date=date(2026, 3, 15))
        result = get_medical_summary(auditor.id, 2026)
        assert result["total_paid"] == 0


# =================================================================
# get_monthly_comparison
# =================================================================


class TestGetMonthlyComparison:
    def test_empty(self, db, user, accounts):
        """仕訳がない場合"""
        result = get_monthly_comparison(user.id, 2026)
        assert result["expense_accounts"] == []
        assert result["income_accounts"] == []
        assert result["expense_totals"] == [0] * 12
        assert result["income_totals"] == [0] * 12

    def test_expense_by_month(self, db, user, accounts):
        """費用の月別集計"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     3000, entry_date=date(2026, 1, 15))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     5000, entry_date=date(2026, 2, 15))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     4000, entry_date=date(2026, 2, 20))
        result = get_monthly_comparison(user.id, 2026)
        assert len(result["expense_accounts"]) == 1
        food = result["expense_accounts"][0]
        assert food["name"] == "食費"
        assert food["months"][0] == 3000   # 1月
        assert food["months"][1] == 9000   # 2月 (5000+4000)
        assert food["total"] == 12000
        assert result["expense_totals"][0] == 3000
        assert result["expense_totals"][1] == 9000

    def test_income_by_month(self, db, user, accounts):
        """収入の月別集計（貸方 - 借方）"""
        make_journal(db, user.id, accounts["1020"].id, accounts["4010"].id,
                     300000, entry_date=date(2026, 1, 25))
        make_journal(db, user.id, accounts["1020"].id, accounts["4010"].id,
                     320000, entry_date=date(2026, 2, 25))
        result = get_monthly_comparison(user.id, 2026)
        assert len(result["income_accounts"]) == 1
        salary = result["income_accounts"][0]
        assert salary["months"][0] == 300000
        assert salary["months"][1] == 320000
        assert result["income_totals"][0] == 300000

    def test_multiple_expense_accounts(self, db, user, accounts):
        """複数費用科目のピボット"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     3000, entry_date=date(2026, 3, 10))
        make_journal(db, user.id, accounts["5020"].id, accounts["1020"].id,
                     80000, entry_date=date(2026, 3, 1))
        result = get_monthly_comparison(user.id, 2026)
        assert len(result["expense_accounts"]) == 2
        assert result["expense_totals"][2] == 83000  # 3月合計

    def test_sorted_by_code(self, db, user, accounts):
        """科目はコード順にソートされる"""
        make_journal(db, user.id, accounts["5020"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 1))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     2000, entry_date=date(2026, 1, 1))
        result = get_monthly_comparison(user.id, 2026)
        codes = [a["code"] for a in result["expense_accounts"]]
        assert codes == ["5010", "5020"]

    def test_no_account_types(self, db, user):
        """科目区分がない場合のフォールバック"""
        result = get_monthly_comparison(user.id, 2026)
        assert result["expense_accounts"] == []
        assert result["expense_totals"] == [0] * 12

    def test_year_boundary(self, db, user, accounts):
        """年度外の仕訳は含まない"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2025, 12, 31))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     2000, entry_date=date(2026, 1, 1))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     3000, entry_date=date(2027, 1, 1))
        result = get_monthly_comparison(user.id, 2026)
        assert result["expense_accounts"][0]["total"] == 2000


# =================================================================
# get_month_projection
# =================================================================


class TestGetMonthProjection:
    def _make_comparison(self, db, user, accounts):
        """テスト用の月次比較データを作成して返す（モック前に呼ぶこと）"""
        # 1月: 食費 80,000, 住居費 90,000, 給与 300,000
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     80000, entry_date=date(2026, 1, 15))
        make_journal(db, user.id, accounts["5020"].id, accounts["1020"].id,
                     90000, entry_date=date(2026, 1, 1))
        make_journal(db, user.id, accounts["1020"].id, accounts["4010"].id,
                     300000, entry_date=date(2026, 1, 25))
        # 2月: 食費 40,000（途中）
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     40000, entry_date=date(2026, 2, 10))
        make_journal(db, user.id, accounts["1020"].id, accounts["4010"].id,
                     300000, entry_date=date(2026, 2, 25))
        return get_monthly_comparison(user.id, 2026)

    def test_fixed_cost_projection(self, db, user, accounts):
        """固定費は前月実績を予測値とする"""
        accounts["5020"].cost_type = "fixed"
        db.session.commit()
        comparison = self._make_comparison(db, user, accounts)

        with patch("app.services.tax.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            result = get_month_projection(user.id, 2026, 2, comparison)

        housing = [p for p in result["expense_projected"] if p["name"] == "住居費"][0]
        assert housing["projected"] == 90000

    def test_variable_cost_projection(self, db, user, accounts):
        """変動費は経過日数で按分"""
        accounts["5010"].cost_type = "variable"
        db.session.commit()
        comparison = self._make_comparison(db, user, accounts)

        with patch("app.services.tax.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 14)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            result = get_month_projection(user.id, 2026, 2, comparison)

        food = [p for p in result["expense_projected"] if p["name"] == "食費"][0]
        # 2月: 14日経過/28日, 実績40,000 → 40000 * 28 / 14 = 80,000
        assert food["actual"] == 40000
        assert food["projected"] == 80000

    def test_occasional_cost_projection(self, db, user, accounts):
        """随時費は実績をそのまま採用"""
        comparison = self._make_comparison(db, user, accounts)

        with patch("app.services.tax.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            result = get_month_projection(user.id, 2026, 2, comparison)

        # cost_type=None → occasional → 実績そのまま
        food = [p for p in result["expense_projected"] if p["name"] == "食費"][0]
        assert food["projected"] == food["actual"]

    def test_projection_metadata(self, db, user, accounts):
        """着地予想のメタデータ"""
        comparison = self._make_comparison(db, user, accounts)

        with patch("app.services.tax.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 20)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            result = get_month_projection(user.id, 2026, 2, comparison)

        assert result["month"] == 2
        assert result["days_elapsed"] == 20
        assert result["days_in_month"] == 28


# =================================================================
# get_income_expense_summary
# =================================================================


class TestGetIncomeExpenseSummary:
    def test_empty(self, db, user, accounts):
        """仕訳がない場合"""
        result = get_income_expense_summary(user.id, 2026)
        assert result["income"] == 0
        assert result["expense"] == 0
        assert result["balance"] == 0

    def test_annual_summary(self, db, user, accounts):
        """年間収支"""
        make_journal(db, user.id, accounts["1020"].id, accounts["4010"].id,
                     300000, entry_date=date(2026, 1, 25))
        make_journal(db, user.id, accounts["1020"].id, accounts["4010"].id,
                     300000, entry_date=date(2026, 2, 25))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     50000, entry_date=date(2026, 1, 15))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     60000, entry_date=date(2026, 2, 15))
        result = get_income_expense_summary(user.id, 2026)
        assert result["income"] == 600000
        assert result["expense"] == 110000
        assert result["balance"] == 490000

    def test_monthly_summary(self, db, user, accounts):
        """月次収支（指定月のみ）"""
        make_journal(db, user.id, accounts["1020"].id, accounts["4010"].id,
                     300000, entry_date=date(2026, 1, 25))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     50000, entry_date=date(2026, 1, 15))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     60000, entry_date=date(2026, 2, 15))
        result = get_income_expense_summary(user.id, 2026, month=1)
        assert result["income"] == 300000
        assert result["expense"] == 50000
        assert result["balance"] == 250000

    def test_december_summary(self, db, user, accounts):
        """12月の月次集計（年跨ぎ境界）"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     30000, entry_date=date(2026, 12, 15))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     10000, entry_date=date(2027, 1, 1))
        result = get_income_expense_summary(user.id, 2026, month=12)
        assert result["expense"] == 30000  # 1月分は含まない

    def test_no_account_types(self, db, user):
        """科目区分がない場合のフォールバック"""
        result = get_income_expense_summary(user.id, 2026)
        assert result["income"] == Decimal(0)
        assert result["balance"] == Decimal(0)

    def test_user_isolation(self, db, user, accounts, auditor):
        """他ユーザーのデータは含まれない"""
        make_journal(db, user.id, accounts["1020"].id, accounts["4010"].id,
                     300000, entry_date=date(2026, 1, 25))
        result = get_income_expense_summary(auditor.id, 2026)
        assert result["income"] == 0
