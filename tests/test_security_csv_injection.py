"""CSV 数式インジェクション対策テスト。"""

from datetime import date

import pytest

from app.views.helpers import csv_safe
from app.models.account import Account
from app.models.medical import MedicalExpense
from tests.conftest import make_journal


@pytest.fixture
def medical_account(db, user, account_types):
    a = Account(user_id=user.id, account_type_id=account_types["expense"].id,
                code="6010", name="医療費", tax_category="medical",
                is_active=True, display_order=200)
    db.session.add(a)
    db.session.commit()
    return a


class TestCsvSafe:
    def test_formula_prefixes_escaped(self):
        for prefix in ("=", "+", "-", "@", "\t", "\r", "\n"):
            assert csv_safe(prefix + "cmd") == "'" + prefix + "cmd"

    def test_normal_text_untouched(self):
        assert csv_safe("山田歯科") == "山田歯科"
        assert csv_safe("") == ""
        assert csv_safe(None) == ""

    def test_inner_symbols_untouched(self):
        # 先頭以外の記号はそのまま (数式にならない)
        assert csv_safe("a=b") == "a=b"


class TestMedicalCsvExport:
    def test_dangerous_names_neutralized(
        self, logged_in_client, db, user, accounts, medical_account
    ):
        """医療費CSVで病院名・受診者名の数式が無害化されること。"""
        entry = make_journal(db, user.id, "6010", "1010", 5000,
                             entry_date=date(2026, 5, 15),
                             description="医療費")
        db.session.add(MedicalExpense(
            user_id=user.id, journal_entry_id=entry.id,
            date=date(2026, 5, 15),
            patient_name="=1+1", hospital_name="@SUM(A1:A2)",
            treatment_description="風邪",
            provider_type="hospital",
            amount_paid=5000, insurance_reimbursement=0,
        ))
        db.session.commit()

        resp = logged_in_client.get("/reports/tax/medical-csv?year=2026")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # 危険な先頭文字はエスケープされている
        assert "'=1+1" in body
        assert "'@SUM(A1:A2)" in body
        # 生の数式としては現れない
        assert ",=1+1" not in body
