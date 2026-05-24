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


class TestCsvTransferDeposit:
    """振替: deposit > 0 (入金) パス"""
    def test_transfer_with_deposit(self, db, logged_in_client, user, accounts):
        _setup_csv_mapping(logged_in_client)
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "口座移動",
             "deposit": 5000, "withdrawal": 0, "category_code": "1020"},
        ]
        resp = logged_in_client.post("/csv-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="csv"
        ).count() == 1


class TestOfxTransferAndOldYear:
    def test_transfer_withdrawal(self, db, logged_in_client, user, accounts):
        with patch("app.views.ofx_import.parse_ofx") as mock_p:
            mock_p.return_value = {
                "account_id": "x",
                "rows": [{"date": "2026-02-15", "description": "x",
                          "deposit": 0, "withdrawal": 1000}],
            }
            logged_in_client.post("/ofx-import/", data={
                "ofx_file": (io.BytesIO(b"x"), "x.ofx"),
                "payment_account_code": "1010",
            }, content_type="multipart/form-data")
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "口座移動",
             "deposit": 0, "withdrawal": 1000, "category_code": "1020"},  # 1020 = asset
        ]
        resp = logged_in_client.post("/ofx-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="ofx"
        ).count() == 1

    def test_transfer_deposit(self, db, logged_in_client, user, accounts):
        with patch("app.views.ofx_import.parse_ofx") as mock_p:
            mock_p.return_value = {
                "account_id": "x",
                "rows": [{"date": "2026-02-15", "description": "x",
                          "deposit": 5000, "withdrawal": 0}],
            }
            logged_in_client.post("/ofx-import/", data={
                "ofx_file": (io.BytesIO(b"x"), "x.ofx"),
                "payment_account_code": "1010",
            }, content_type="multipart/form-data")
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "入金",
             "deposit": 5000, "withdrawal": 0, "category_code": "1020"},
        ]
        resp = logged_in_client.post("/ofx-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="ofx"
        ).count() == 1

    def test_old_year_capital_conversion(self, db, logged_in_client, user, accounts):
        """未開設の旧年度行を元入金 (3010) に変換"""
        with patch("app.views.ofx_import.parse_ofx") as mock_p:
            mock_p.return_value = {
                "account_id": "x",
                "rows": [{"date": "2023-05-15", "description": "古い取引",
                          "deposit": 0, "withdrawal": 1000}],
            }
            logged_in_client.post("/ofx-import/", data={
                "ofx_file": (io.BytesIO(b"x"), "x.ofx"),
                "payment_account_code": "1010",
            }, content_type="multipart/form-data")
        rows = [
            {"enabled": True, "date": "2023-05-15", "description": "古い取引",
             "deposit": 0, "withdrawal": 1000, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/ofx-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "capital",  # 元入金変換
        })
        assert resp.status_code in (302, 303)
        # 1件取込（元入金で変換）
        entries = JournalEntry.query.filter_by(
            user_id=user.id, source="ofx"
        ).all()
        assert len(entries) == 1
        # 摘要に元の日付が付与される
        assert "2023-05-15" in entries[0].description

    def test_old_year_skip(self, db, logged_in_client, user, accounts):
        with patch("app.views.ofx_import.parse_ofx") as mock_p:
            mock_p.return_value = {
                "account_id": "x",
                "rows": [{"date": "2023-05-15", "description": "古い",
                          "deposit": 0, "withdrawal": 1000}],
            }
            logged_in_client.post("/ofx-import/", data={
                "ofx_file": (io.BytesIO(b"x"), "x.ofx"),
                "payment_account_code": "1010",
            }, content_type="multipart/form-data")
        rows = [
            {"enabled": True, "date": "2023-05-15", "description": "古い",
             "deposit": 0, "withdrawal": 1000, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/ofx-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        # 未開設年度なのでスキップ
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="ofx"
        ).count() == 0

    def test_locked_period_skipped(self, db, logged_in_client, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        with patch("app.views.ofx_import.parse_ofx") as mock_p:
            mock_p.return_value = {
                "account_id": "x",
                "rows": [{"date": "2026-02-15", "description": "x",
                          "deposit": 0, "withdrawal": 100}],
            }
            logged_in_client.post("/ofx-import/", data={
                "ofx_file": (io.BytesIO(b"x"), "x.ofx"),
                "payment_account_code": "1010",
            }, content_type="multipart/form-data")
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "x",
             "deposit": 0, "withdrawal": 100, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/ofx-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="ofx"
        ).count() == 0

    def test_missing_date_skipped(self, db, logged_in_client, user, accounts):
        with patch("app.views.ofx_import.parse_ofx") as mock_p:
            mock_p.return_value = {
                "account_id": "x",
                "rows": [{"description": "x", "deposit": 0, "withdrawal": 100}],
            }
            logged_in_client.post("/ofx-import/", data={
                "ofx_file": (io.BytesIO(b"x"), "x.ofx"),
                "payment_account_code": "1010",
            }, content_type="multipart/form-data")
        rows = [
            {"enabled": True, "date": "", "description": "x",
             "deposit": 0, "withdrawal": 100, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/ofx-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="ofx"
        ).count() == 0


class TestWebTransferAndOldYear:
    """E2 PR-C-4h: POST /web-import/ は機能停止。session を直接仕込んで
    confirm フロー (仕訳生成) のみをテスト。"""

    # E2 PR-C-4i: 旧 _seed と test_imports._setup_web_import_session の重複を
    # 解消。test_imports 側のヘルパーを再利用。
    @staticmethod
    def _seed(logged_in_client, parsed_rows):
        from tests.test_imports import _setup_web_import_session
        _setup_web_import_session(logged_in_client, parsed_rows)

    def test_transfer_withdrawal(self, db, logged_in_client, user, accounts):
        self._seed(logged_in_client, [{
            "date": "2026-02-15", "description": "口座移動",
            "deposit": 0, "withdrawal": 5000,
        }])
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "口座移動",
             "deposit": 0, "withdrawal": 5000, "category_code": "1020"},
        ]
        resp = logged_in_client.post("/web-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="web"
        ).count() == 1

    def test_transfer_deposit(self, db, logged_in_client, user, accounts):
        self._seed(logged_in_client, [{
            "date": "2026-02-15", "description": "入金",
            "deposit": 5000, "withdrawal": 0,
        }])
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "入金",
             "deposit": 5000, "withdrawal": 0, "category_code": "1020"},
        ]
        resp = logged_in_client.post("/web-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="web"
        ).count() == 1

    def test_old_year_capital(self, db, logged_in_client, user, accounts):
        self._seed(logged_in_client, [{
            "date": "2023-05-15", "description": "古い",
            "deposit": 0, "withdrawal": 1000,
        }])
        rows = [
            {"enabled": True, "date": "2023-05-15", "description": "古い",
             "deposit": 0, "withdrawal": 1000, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/web-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "capital",
        })
        assert resp.status_code in (302, 303)
        entries = JournalEntry.query.filter_by(
            user_id=user.id, source="web"
        ).all()
        assert len(entries) == 1
        assert "2023-05-15" in entries[0].description

    def test_locked_period_skipped(self, db, logged_in_client, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        self._seed(logged_in_client, [{
            "date": "2026-02-15", "description": "x",
            "deposit": 0, "withdrawal": 100,
        }])
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "x",
             "deposit": 0, "withdrawal": 100, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/web-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="web"
        ).count() == 0


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
        """残高キャッシュが効いている年度のレポート表示"""
        from app.models.balance_cache import BalanceCache
        from app.models.fiscal import FiscalClose
        # 2024-2 を確定 + キャッシュ
        db.session.add(FiscalClose(user_id=user.id, year=2024, closed_period=2))
        db.session.add(BalanceCache(
            user_id=user.id, year=2024, period=2,
            account_code="5010",
            cumulative_debit=10000, cumulative_credit=0,
        ))
        db.session.commit()
        # 2024 年 3 月のレポートを表示 (period_from=3 → cache 利用)
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
        from app.models.fiscal import FiscalClose
        from app.models.balance_cache import BalanceCache
        # 2026-1 を確定 + キャッシュ
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.add(BalanceCache(
            user_id=user.id, year=2026, period=1,
            account_code="5010",
            cumulative_debit=5000, cumulative_credit=0,
        ))
        db.session.commit()
        resp = logged_in_client.get("/reports/monthly?year=2026")
        assert resp.status_code == 200
