"""医療費管理 (medical.py) のテスト"""

from datetime import date

import pytest

from app.models.account import Account
from app.models.medical import MedicalExpense


@pytest.fixture
def medical_account(db, user, account_types, accounts):
    """医療費科目 (6010, tax_category=medical) を追加"""
    a = Account(
        user_id=user.id,
        account_type_id=account_types["expense"].id,
        code="6010", name="医療費",
        tax_category="medical",
        is_active=True, display_order=100,
    )
    db.session.add(a)
    db.session.commit()
    return a


class TestIndex:
    def test_unauthenticated(self, client):
        resp = client.get("/medical/")
        assert resp.status_code in (302, 401)

    def test_empty(self, logged_in_client, accounts, medical_account):
        resp = logged_in_client.get("/medical/")
        assert resp.status_code == 200

    def test_year_filter(self, logged_in_client, accounts, medical_account):
        resp = logged_in_client.get("/medical/?year=2024")
        assert resp.status_code == 200


class TestNew:
    def test_unauthenticated(self, client):
        resp = client.get("/medical/new")
        assert resp.status_code in (302, 401)

    def test_get_form(self, logged_in_client, accounts, medical_account):
        resp = logged_in_client.get("/medical/new")
        assert resp.status_code == 200

    def test_create_success(self, db, logged_in_client, user, accounts, medical_account):
        resp = logged_in_client.post("/medical/new", data={
            "date": "2026-02-15",
            "patient_name": "本人",
            "hospital_name": "○○病院",
            "treatment_description": "風邪",
            "amount_paid": "5000",
            "insurance_reimbursement": "0",
            "payment_account_code": "1010",
        })
        assert resp.status_code in (302, 303)
        # MedicalExpense と JournalEntry が作成される
        assert MedicalExpense.query.filter_by(user_id=user.id).count() == 1

    def test_create_without_medical_account(self, db, logged_in_client, user, accounts):
        """6010 科目が存在しないとエラー"""
        resp = logged_in_client.post("/medical/new", data={
            "date": "2026-02-15",
            "patient_name": "X",
            "hospital_name": "Y",
            "treatment_description": "Z",
            "amount_paid": "1000",
            "insurance_reimbursement": "0",
            "payment_account_code": "1010",
        })
        # form 再表示
        assert resp.status_code == 200
        assert MedicalExpense.query.filter_by(user_id=user.id).count() == 0


class TestEdit:
    def _make_expense(self, db, user_id, medical_account):
        from app.services.accounting import create_cashbook_entry
        entry = create_cashbook_entry(
            user_id=user_id,
            date=date(2026, 2, 15),
            transaction_type="expense",
            payment_account_code="1010",
            category_account_code=medical_account.code,
            amount=3000,
            description="医療費",
        )
        e = MedicalExpense(
            user_id=user_id, journal_entry_id=entry.id,
            date=date(2026, 2, 15),
            patient_name="本人", hospital_name="○○病院",
            treatment_description="風邪",
            amount_paid=3000, insurance_reimbursement=0,
        )
        db.session.add(e)
        db.session.commit()
        return e

    def test_unauthenticated(self, client):
        resp = client.get("/medical/1/edit")
        assert resp.status_code in (302, 401)

    def test_404_for_nonexistent(self, logged_in_client, accounts, medical_account):
        resp = logged_in_client.get("/medical/9999/edit")
        assert resp.status_code == 404

    def test_get_form(self, db, logged_in_client, user, accounts, medical_account):
        e = self._make_expense(db, user.id, medical_account)
        resp = logged_in_client.get(f"/medical/{e.id}/edit")
        assert resp.status_code == 200

    def test_idor_other_user(self, db, logged_in_client, user, accounts,
                             medical_account, second_user, second_user_accounts):
        # 他人の medical 6010 も作る必要があるので、journal だけ作って expense を作る
        from app.models.journal import JournalEntry, JournalEntryLine
        je = JournalEntry(
            user_id=second_user.id, date=date(2026, 2, 15),
            entry_number=1, description="x", source="cashbook",
        )
        je.lines = [
            JournalEntryLine(account_user_id=second_user.id,
                             account_code="5010", debit_amount=100,
                             credit_amount=0),
            JournalEntryLine(account_user_id=second_user.id,
                             account_code="1010", debit_amount=0,
                             credit_amount=100),
        ]
        db.session.add(je)
        db.session.commit()
        e = MedicalExpense(
            user_id=second_user.id, journal_entry_id=je.id,
            date=date(2026, 2, 15),
            patient_name="他人", hospital_name="他病院",
            treatment_description="x",
            amount_paid=100, insurance_reimbursement=0,
        )
        db.session.add(e)
        db.session.commit()
        resp = logged_in_client.get(f"/medical/{e.id}/edit")
        assert resp.status_code == 404

    def test_post_updates(self, db, logged_in_client, user, accounts, medical_account):
        e = self._make_expense(db, user.id, medical_account)
        resp = logged_in_client.post(f"/medical/{e.id}/edit", data={
            "date": "2026-02-20",
            "patient_name": "更新後",
            "hospital_name": "××病院",
            "treatment_description": "別件",
            "amount_paid": "5000",
            "insurance_reimbursement": "1000",
            "payment_account_code": "1010",
        })
        assert resp.status_code in (302, 303)
        db.session.refresh(e)
        assert e.patient_name == "更新後"
        assert e.insurance_reimbursement == 1000


class TestDelete:
    def _make_expense(self, db, user_id, medical_account):
        from app.services.accounting import create_cashbook_entry
        entry = create_cashbook_entry(
            user_id=user_id, date=date(2026, 2, 15),
            transaction_type="expense",
            payment_account_code="1010",
            category_account_code=medical_account.code,
            amount=2000, description="医療費",
        )
        e = MedicalExpense(
            user_id=user_id, journal_entry_id=entry.id,
            date=date(2026, 2, 15),
            patient_name="X", hospital_name="Y",
            treatment_description="Z",
            amount_paid=2000, insurance_reimbursement=0,
        )
        db.session.add(e)
        db.session.commit()
        return e

    def test_unauthenticated(self, client):
        resp = client.post("/medical/1/delete")
        assert resp.status_code in (302, 401)

    def test_delete_success(self, db, logged_in_client, user, accounts, medical_account):
        e = self._make_expense(db, user.id, medical_account)
        eid = e.id
        resp = logged_in_client.post(f"/medical/{eid}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(MedicalExpense, eid) is None

    def test_delete_blocked_by_closed_period(
        self, db, logged_in_client, user, accounts, medical_account,
    ):
        """確定済み期間の医療費削除は仕訳ロックでブロック (flash + redirect)。"""
        from app.models.fiscal import FiscalClose
        e = self._make_expense(db, user.id, medical_account)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(f"/medical/{e.id}/delete", follow_redirects=False)
        assert resp.status_code in (302, 303)
        # 削除されずに残る
        assert db.session.get(MedicalExpense, e.id) is not None


class TestNewWithClosedPeriod:
    """新規作成時の確定済み期間ガード。"""

    def test_post_to_closed_period_rerenders(
        self, db, logged_in_client, user, accounts, medical_account,
    ):
        from app.models.fiscal import FiscalClose
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post("/medical/new", data={
            "date": "2026-02-10",  # 確定済み期間
            "patient_name": "本人",
            "hospital_name": "病院",
            "treatment_description": "検査",
            "amount_paid": "1000",
            "insurance_reimbursement": "0",
            "payment_account_code": "1010",
        })
        # 確定済みエラーでフォーム再描画 (200)
        assert resp.status_code == 200


class TestEditPrefillPaymentAccount:
    """edit GET で payment_account_code 由来の科目名表示パス (line 222-223)。"""

    def test_edit_get_renders_payment_account_name(
        self, db, logged_in_client, user, accounts, medical_account,
    ):
        from app.services.accounting import create_cashbook_entry
        entry = create_cashbook_entry(
            user_id=user.id, date=date(2026, 2, 15),
            transaction_type="expense",
            payment_account_code="1010",
            category_account_code=medical_account.code,
            amount=4000, description="医療費",
        )
        e = MedicalExpense(
            user_id=user.id, journal_entry_id=entry.id,
            date=date(2026, 2, 15),
            patient_name="本人", hospital_name="病院",
            treatment_description="x",
            amount_paid=4000, insurance_reimbursement=0,
        )
        db.session.add(e)
        db.session.commit()
        # POST で payment_account_code を送信し、バリデーション通って再描画される
        # (= form.payment_account_code.data が埋まる) パスを通す
        resp = logged_in_client.post(f"/medical/{e.id}/edit", data={
            "date": "",  # required 欠落
            "patient_name": "更新",
            "hospital_name": "更新",
            "treatment_description": "x",
            "amount_paid": "4000",
            "insurance_reimbursement": "0",
            "payment_account_code": "1010",
        })
        # バリデーション失敗で 200 (フォーム再描画) — 科目名 lookup 経路を通る
        assert resp.status_code == 200


class TestApiGet:
    def test_unauthenticated(self, client):
        resp = client.get("/medical/api/1")
        assert resp.status_code in (302, 401)

    def test_empty_for_entry_without_medical(self, db, logged_in_client, user, accounts):
        from tests.conftest import make_journal
        e = make_journal(db, user.id, "5010", "1010", 100,
                         entry_date=date(2026, 2, 15), source="cashbook")
        resp = logged_in_client.get(f"/medical/api/{e.id}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["entry_id"] == e.id
        assert body["patient_name"] == ""

    def test_idor_other_user(self, db, logged_in_client, user, accounts,
                             second_user, second_user_accounts):
        from tests.conftest import make_journal
        other = make_journal(
            db, second_user.id, "5010", "1010", 100,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        resp = logged_in_client.get(f"/medical/api/{other.id}")
        assert resp.status_code == 404


class TestApiUpdate:
    def test_unauthenticated(self, client):
        resp = client.post("/medical/api/1", json={})
        assert resp.status_code in (302, 401)

    def test_create_new_record(self, db, logged_in_client, user, accounts, medical_account):
        from app.services.accounting import create_cashbook_entry
        entry = create_cashbook_entry(
            user_id=user.id, date=date(2026, 2, 15),
            transaction_type="expense",
            payment_account_code="1010",
            category_account_code=medical_account.code,
            amount=2500, description="x",
        )
        resp = logged_in_client.post(f"/medical/api/{entry.id}", json={
            "patient_name": "本人",
            "hospital_name": "病院",
            "provider_type": "hospital",
            "treatment_description": "通院",
            "insurance_reimbursement": 500,
        })
        assert resp.status_code == 200
        me = MedicalExpense.query.filter_by(journal_entry_id=entry.id).first()
        assert me is not None
        assert me.patient_name == "本人"
        assert me.amount_paid == 2500  # 仕訳の医療費科目借方合計

    def test_update_existing(self, db, logged_in_client, user, accounts, medical_account):
        from app.services.accounting import create_cashbook_entry
        entry = create_cashbook_entry(
            user_id=user.id, date=date(2026, 2, 15),
            transaction_type="expense",
            payment_account_code="1010",
            category_account_code=medical_account.code,
            amount=1500, description="x",
        )
        me = MedicalExpense(
            user_id=user.id, journal_entry_id=entry.id,
            date=date(2026, 2, 15),
            patient_name="旧", hospital_name="旧",
            treatment_description="旧",
            amount_paid=1500, insurance_reimbursement=0,
        )
        db.session.add(me)
        db.session.commit()
        resp = logged_in_client.post(f"/medical/api/{entry.id}", json={
            "patient_name": "新",
            "hospital_name": "新病院",
            "treatment_description": "新治療",
            "insurance_reimbursement": 200,
        })
        assert resp.status_code == 200
        db.session.refresh(me)
        assert me.patient_name == "新"


class TestApiSuggestions:
    def test_unauthenticated(self, client):
        resp = client.get("/medical/api/suggestions")
        assert resp.status_code in (302, 401)

    def test_returns_distinct_suggestions(self, db, logged_in_client, user, accounts):
        # 仕訳と紐付かない MedicalExpense でも OK
        for n in ["太郎", "花子", "太郎"]:
            db.session.add(MedicalExpense(
                user_id=user.id, date=date(2026, 1, 1),
                patient_name=n, hospital_name="X",
                treatment_description="t", amount_paid=100,
                insurance_reimbursement=0,
            ))
        db.session.commit()
        resp = logged_in_client.get("/medical/api/suggestions")
        body = resp.get_json()
        # 重複は排除
        assert sorted(body["patients"]) == ["太郎", "花子"]
