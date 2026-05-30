"""医療費管理 (medical.py) のテスト。

Phase E3-F PR-D-3 で医療費 UI はクライアント描画 + クライアント暗号化に移行。
- 一覧 (index) / 新規フォーム (new) は GET のサーバレンダ shell のみ。
- 保存は /api/v1/medical-expenses (POST) + /api/v1/journals/batch (作成時)
  経由 (tests/test_api.py の TestMedicalExpensesUpsert を参照)。
- 削除 (delete) はサーバ側に残る (平文を読まない、伝票ロック判定のみ)。
"""

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
        # クライアント描画用の params / accounts-meta が埋め込まれる
        assert b"medical-index-params" in resp.data
        assert b"medical-index-accounts-meta" in resp.data

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
        # JS submit 用に医療費科目コードが埋め込まれる
        assert b"_medicalAccountCode" in resp.data

    def test_get_form_without_medical_account(self, logged_in_client, accounts):
        """6010 科目がなくてもフォーム自体は開ける (JS 側で submit を弾く)。"""
        resp = logged_in_client.get("/medical/new")
        assert resp.status_code == 200

    def test_post_not_allowed(self, logged_in_client, accounts, medical_account):
        """新規作成はクライアント完結。サーバ側 POST は撤去済 (405)。"""
        resp = logged_in_client.post("/medical/new", data={"date": "2026-02-15"})
        assert resp.status_code == 405


class TestDelete:
    def _make_expense(self, db, user_id, medical_account):
        from app.services.accounting import create_journal_entry
        entry = create_journal_entry(
            user_id=user_id, date=date(2026, 2, 15),
            description="医療費",
            lines_data=[
                {"account_code": medical_account.code,
                 "debit_amount": 2000, "credit_amount": 0},
                {"account_code": "1010",
                 "debit_amount": 0, "credit_amount": 2000},
            ],
            source="cashbook",
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

    def test_404_for_nonexistent(self, logged_in_client, accounts, medical_account):
        resp = logged_in_client.post("/medical/9999/delete")
        assert resp.status_code == 404

    def test_delete_success(self, db, logged_in_client, user, accounts, medical_account):
        e = self._make_expense(db, user.id, medical_account)
        eid = e.id
        resp = logged_in_client.post(f"/medical/{eid}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(MedicalExpense, eid) is None

    def test_idor_other_user(self, db, logged_in_client, user, accounts,
                             second_user, second_user_accounts):
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
        resp = logged_in_client.post(f"/medical/{e.id}/delete")
        assert resp.status_code == 404

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
