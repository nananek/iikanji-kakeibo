"""Lv2 顧問の編集スコープ強制 (BOLA/権限昇格) の回帰テスト。

Lv2 は公開科目 (AuditGrantAccount) のみ編集できる設計。フォーム経由
(`journal.new` / `journal.edit`) は許可科目を検証しているが、JSON API
(`create_api` / `edit_api`) でも同じ強制が必要。
"""

import json
from datetime import date

import pytest

from app.models.audit import AuditGrant, AuditGrantAccount
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.account import Account


@pytest.fixture
def lv2_setup(db, client, user, auditor, accounts):
    """Lv2 顧問が user を代理閲覧する状態。(5010, 1010, 3030) のみ公開。"""
    grant = AuditGrant(
        owner_user_id=user.id,
        auditor_user_id=auditor.id,
        permission_level=2,
        status="active",
    )
    db.session.add(grant)
    db.session.flush()
    db.session.add_all([
        AuditGrantAccount(audit_grant_id=grant.id,
                          account_user_id=user.id, account_code="5010"),
        AuditGrantAccount(audit_grant_id=grant.id,
                          account_user_id=user.id, account_code="1010"),
        AuditGrantAccount(audit_grant_id=grant.id,
                          account_user_id=user.id, account_code="3030"),
    ])
    db.session.commit()

    with client.session_transaction() as sess:
        sess["_user_id"] = str(auditor.id)
        sess["acting_as_user_id"] = user.id
        sess["acting_as_permission_level"] = 2
    return grant


@pytest.fixture
def visible_only_journal(db, user):
    """公開科目のみで構成された仕訳 (非公開行なし)"""
    e = JournalEntry(
        user_id=user.id, date=date(2026, 2, 15),
        entry_number=1, description="公開科目のみ", source="journal",
    )
    e.lines = [
        JournalEntryLine(account_user_id=user.id, account_code="5010",
                         debit_amount=1000, credit_amount=0),
        JournalEntryLine(account_user_id=user.id, account_code="1010",
                         debit_amount=0, credit_amount=1000),
    ]
    db.session.add(e)
    db.session.commit()
    return e


class TestLv2ApiScopeEnforcement:
    def test_edit_api_rejects_hidden_account_code(
            self, lv2_setup, visible_only_journal, client, db):
        """edit-api で非公開科目 (5020) を書き込めないこと。"""
        resp = client.post(
            f"/journal/{visible_only_journal.id}/edit-api",
            json={
                "date": "2026-02-15",
                "description": "scope bypass attempt",
                "lines": [
                    {"account_code": "5020", "debit_amount": 1000,
                     "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0,
                     "credit_amount": 1000},
                ],
            },
        )
        assert resp.status_code in (400, 403)
        e = db.session.get(JournalEntry, visible_only_journal.id)
        codes = {l.account_code for l in e.lines}
        assert "5020" not in codes

    def test_create_api_rejects_hidden_account_code(
            self, lv2_setup, client, db, user):
        """create-api で非公開科目 (5020) の仕訳を作成できないこと。"""
        resp = client.post(
            "/journal/create-api",
            json={
                "date": "2026-02-15",
                "description": "scope bypass attempt",
                "lines": [
                    {"account_code": "5020", "debit_amount": 1000,
                     "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0,
                     "credit_amount": 1000},
                ],
            },
        )
        assert resp.status_code in (400, 403)
        created = (
            JournalEntry.query
            .filter_by(user_id=user.id, description="scope bypass attempt")
            .all()
        )
        assert created == []

    def test_edit_form_rejects_hidden_account_code(
            self, lv2_setup, visible_only_journal, client, db):
        """フォーム編集 (/journal/<id>/edit) でも非公開科目を書き込めないこと。"""
        resp = client.post(
            f"/journal/{visible_only_journal.id}/edit",
            data={
                "date": "2026-02-15",
                "description": "scope bypass attempt",
                "fiscal_period": "",
                "lines_json": json.dumps([
                    {"account_code": "5020", "debit_amount": 1000,
                     "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0,
                     "credit_amount": 1000},
                ]),
            },
        )
        # 成功 (302) してはならない。フォーム再表示 or エラー。
        assert resp.status_code in (200, 400, 403)
        e = db.session.get(JournalEntry, visible_only_journal.id)
        codes = {l.account_code for l in e.lines}
        assert "5020" not in codes


class TestLv2BatchDeleteScope:
    def test_delete_batch_keeps_entries_with_hidden_accounts(
            self, lv2_setup, client, db, user):
        """Lv2 顧問は非公開科目を含むバッチを削除できないこと。"""
        e = JournalEntry(
            user_id=user.id, date=date(2026, 2, 15), entry_number=50,
            description="batch entry", source="csv", batch_id="batch-1",
        )
        e.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="5020",
                             debit_amount=1000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=0, credit_amount=1000),
        ]
        db.session.add(e)
        db.session.commit()
        entry_id = e.id

        resp = client.post("/journal/batches/batch-1/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(JournalEntry, entry_id) is not None


class TestLv2CashbookScopeEnforcement:
    def test_cashbook_new_rejects_hidden_account_code(
            self, lv2_setup, client, db, user):
        """出納帳の新規作成で非公開科目 (5020) を費目にできないこと。"""
        resp = client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "fiscal_period": "",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5020",
            "amount": "1000",
            "description": "scope bypass attempt",
        })
        assert resp.status_code in (200, 400, 403)
        created = (
            JournalEntry.query
            .filter_by(user_id=user.id, source="cashbook",
                       description="scope bypass attempt")
            .all()
        )
        assert created == []

    def test_cashbook_edit_rejects_hidden_account_code(
            self, lv2_setup, client, db, user):
        """出納帳の編集で非公開科目 (5020) に付け替えられないこと。"""
        e = JournalEntry(
            user_id=user.id, date=date(2026, 2, 15),
            entry_number=2, description="元の支出", source="cashbook",
        )
        e.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=1000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=0, credit_amount=1000),
        ]
        db.session.add(e)
        db.session.commit()

        resp = client.post(f"/cashbook/{e.id}/edit", data={
            "date": "2026-02-15",
            "fiscal_period": "",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5020",
            "amount": "1000",
            "description": "scope bypass attempt",
        })
        assert resp.status_code in (200, 400, 403)
        db.session.refresh(e)
        codes = {l.account_code for l in e.lines}
        assert "5020" not in codes


class TestLv2MedicalScopeEnforcement:
    def test_medical_new_rejects_hidden_payment_account(
            self, db, client, user, auditor, accounts, account_types):
        """医療費登録で非公開の支払元科目を使えないこと。"""
        # 医療費科目 (6010) を公開科目に含む Lv2 グラント
        db.session.add(Account(
            user_id=user.id, account_type_id=account_types["expense"].id,
            code="6010", name="医療費", tax_category="medical",
            is_active=True, display_order=100,
        ))
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="active",
        )
        db.session.add(grant)
        db.session.flush()
        db.session.add_all([
            AuditGrantAccount(audit_grant_id=grant.id,
                              account_user_id=user.id, account_code="6010"),
            AuditGrantAccount(audit_grant_id=grant.id,
                              account_user_id=user.id, account_code="1010"),
            AuditGrantAccount(audit_grant_id=grant.id,
                              account_user_id=user.id, account_code="3030"),
        ])
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 2

        resp = client.post("/medical/new", data={
            "date": "2026-02-15",
            "patient_name": "本人",
            "hospital_name": "病院",
            "treatment_description": "診察",
            "amount_paid": "1000",
            "insurance_reimbursement": "0",
            "payment_account_code": "5020",
        })
        assert resp.status_code in (200, 400, 403)
        from app.models.medical import MedicalExpense
        created = (
            MedicalExpense.query
            .filter_by(user_id=user.id, hospital_name="病院")
            .all()
        )
        assert created == []
