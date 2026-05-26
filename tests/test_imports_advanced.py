"""CSV/OFX/Web 取込の高度なエッジケース

振替仕訳判定、未開設年度→元入金変換、ロック科目スキップ、確定済み期間スキップ。
"""

import io
import json
from datetime import date
from unittest.mock import patch

from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry


def _setup_csv(client, csv_bytes=None, payment="1010"):
    if csv_bytes is None:
        csv_bytes = ("日付,摘要,出金,入金\n2026-02-15,x,100,0\n").encode("utf-8")
    return client.post("/csv-import/", data={
        "csv_file": (io.BytesIO(csv_bytes), "x.csv"),
        "payment_account_code": payment,
    }, content_type="multipart/form-data")


def _setup_csv_mapping(client):
    _setup_csv(client)
    client.post("/csv-import/mapping", data={
        "date_col": "0", "desc_col": "1",
        "withdrawal_col": "2", "deposit_col": "3",
        "date_format": "%Y-%m-%d",
    })


class TestCashbookLockedAccounts:
    """確定済み Lv2 ロック科目の挙動"""

    def _setup_locked(self, db, user, auditor):
        """user の 5010 を提出済み Lv2 で公開 → ロック状態"""
        from app.models.audit import AuditGrant, AuditGrantAccount
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=2,
            status="submitted",
        )
        db.session.add(grant)
        db.session.flush()
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id,
            account_user_id=user.id, account_code="5010",
        ))
        db.session.commit()
        return grant

    def test_new_with_locked_account_blocked(self, db, logged_in_client, user, accounts, auditor):
        self._setup_locked(db, user, auditor)
        # 5010 を含む新規仕訳は提出済みなのでブロック
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",  # locked
            "amount": "1000",
            "description": "x",
            "fiscal_period": "",
        })
        assert resp.status_code == 200  # form 再表示
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).count() == 0


class TestMedicalEdgeCases:
    def test_index_with_data(self, db, logged_in_client, user, accounts):
        from app.models.account import Account
        from app.models.medical import MedicalExpense
        # 6010 (medical_expense) 科目を作成
        from app.models.account import AccountType
        expense_type = AccountType.query.filter_by(code="expense").first()
        a = Account(
            user_id=user.id, account_type_id=expense_type.id,
            code="6010", name="医療費",
            tax_category="medical",
            is_active=True, display_order=100,
        )
        db.session.add(a)
        db.session.commit()
        # MedicalExpense + JournalEntry を作る
        from app.services.accounting import create_cashbook_entry
        entry = create_cashbook_entry(
            user_id=user.id, date=date(2026, 2, 15),
            transaction_type="expense",
            payment_account_code="1010",
            category_account_code="6010",
            amount=5000, description="医療費",
        )
        db.session.add(MedicalExpense(
            user_id=user.id, journal_entry_id=entry.id,
            date=date(2026, 2, 15),
            patient_name="本人", hospital_name="○病院",
            treatment_description="風邪",
            amount_paid=5000, insurance_reimbursement=1000,
        ))
        db.session.commit()
        resp = logged_in_client.get("/medical/?year=2026")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "5,000" in body or "5000" in body


class TestReportsBalanceWithCache:
    def test_with_balance_cache(self, db, logged_in_client, user, accounts):
        """E3-F-6: サーバ側残高集計は撤去済 (HTML テンプレ + accountsMeta のみ返却)。
        確定済期間でも HTTP 200 が返ること。"""
        from app.models.fiscal import FiscalClose
        db.session.add(FiscalClose(user_id=user.id, year=2024, closed_period=2))
        db.session.commit()
        resp = logged_in_client.get("/reports/balance?year=2024&period=3")
        assert resp.status_code == 200

    def test_pl_with_data(self, db, logged_in_client, user, accounts):
        from tests.conftest import make_journal
        # 収益と費用を作る
        make_journal(db, user.id, "1010", "4010", 250000,
                     entry_date=date(2026, 2, 25), source="cashbook",
                     description="給与")
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="cashbook",
                     description="食費")
        resp = logged_in_client.get("/reports/pl?year=2026")
        assert resp.status_code == 200

    def test_bs_with_data(self, db, logged_in_client, user, accounts):
        from tests.conftest import make_journal
        make_journal(db, user.id, "1020", "1010", 50000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        resp = logged_in_client.get("/reports/bs?year=2026")
        assert resp.status_code == 200

    def test_ledger_with_account(self, db, logged_in_client, user, accounts):
        from tests.conftest import make_journal
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        resp = logged_in_client.get("/reports/ledger?account_code=5010&year=2026")
        assert resp.status_code == 200

    def test_ledger_with_sort_desc(self, db, logged_in_client, user, accounts):
        from tests.conftest import make_journal
        for d in [date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 15)]:
            make_journal(db, user.id, "5010", "1010", 100,
                         entry_date=d, source="cashbook")
        # ledger_sort_order を desc にして再度取得
        user.set_pref("ledger_sort_order", "desc")
        from app.extensions import db as _db
        _db.session.commit()
        resp = logged_in_client.get("/reports/ledger?account_code=5010&year=2026")
        assert resp.status_code == 200

    def test_monthly_with_cache(self, db, logged_in_client, user, accounts):
        """E3-F-6: 月次比較もクライアント集計に移行済。HTTP 200 のみ確認。"""
        from app.models.fiscal import FiscalClose
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = logged_in_client.get("/reports/monthly?year=2026")
        assert resp.status_code == 200
