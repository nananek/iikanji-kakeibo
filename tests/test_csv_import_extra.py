"""CSV取込ビュー (csv_import.py) の追加テスト

mapping → confirm → 取込実行までのフローと reconcile/ai-reconcile API。
"""

import io
import json
from datetime import date
from unittest.mock import patch

from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry


def _setup_csv_session(client, csv_bytes=None, payment="1010"):
    if csv_bytes is None:
        csv_bytes = (
            "日付,摘要,出金,入金\n"
            "2026-02-15,セブン,500,0\n"
            "2026-02-16,給与,0,250000\n"
        ).encode("utf-8")
    resp = client.post("/csv-import/", data={
        "csv_file": (io.BytesIO(csv_bytes), "x.csv"),
        "payment_account_code": payment,
    }, content_type="multipart/form-data")
    return resp


class TestMappingPost:
    def test_post_with_required_columns(self, db, logged_in_client, user, accounts):
        _setup_csv_session(logged_in_client)
        resp = logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0",
            "desc_col": "1",
            "withdrawal_col": "2",
            "deposit_col": "3",
            "date_format": "%Y-%m-%d",
        })
        assert resp.status_code in (302, 303)
        assert "/csv-import/confirm" in resp.headers["Location"]

    def test_post_missing_required(self, logged_in_client, accounts):
        _setup_csv_session(logged_in_client)
        resp = logged_in_client.post("/csv-import/mapping", data={
            "date_col": "",
            "desc_col": "",
        })
        assert resp.status_code == 200

    def test_post_invalid_mapping_no_data(self, logged_in_client, accounts):
        # date_format ミスでパースできない
        csv = "ABC\nfoo".encode("utf-8")
        _setup_csv_session(logged_in_client, csv_bytes=csv)
        resp = logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0",
            "desc_col": "0",
        })
        assert resp.status_code == 200


class TestConfirm:
    def test_no_data_redirects(self, logged_in_client, accounts):
        resp = logged_in_client.get("/csv-import/confirm")
        assert resp.status_code in (302, 303)

    def test_get_after_full_flow(self, db, logged_in_client, user, accounts):
        _setup_csv_session(logged_in_client)
        logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0", "desc_col": "1",
            "withdrawal_col": "2", "deposit_col": "3",
            "date_format": "%Y-%m-%d",
        })
        resp = logged_in_client.get("/csv-import/confirm")
        assert resp.status_code == 200

    def test_post_imports(self, db, logged_in_client, user, accounts):
        _setup_csv_session(logged_in_client)
        logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0", "desc_col": "1",
            "withdrawal_col": "2", "deposit_col": "3",
            "date_format": "%Y-%m-%d",
        })
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "セブン",
             "deposit": 0, "withdrawal": 500, "category_code": "5010"},
            {"enabled": True, "date": "2026-02-16", "description": "給与",
             "deposit": 250000, "withdrawal": 0, "category_code": "4010"},
        ]
        resp = logged_in_client.post("/csv-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="csv"
        ).count() == 2

    def test_post_no_rows_redirects(self, db, logged_in_client, user, accounts):
        _setup_csv_session(logged_in_client)
        logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0", "desc_col": "1",
            "withdrawal_col": "2", "deposit_col": "3",
            "date_format": "%Y-%m-%d",
        })
        resp = logged_in_client.post("/csv-import/confirm", data={})
        assert resp.status_code in (302, 303)

    def test_disabled_rows_skipped(self, db, logged_in_client, user, accounts):
        _setup_csv_session(logged_in_client)
        logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0", "desc_col": "1",
            "withdrawal_col": "2", "deposit_col": "3",
            "date_format": "%Y-%m-%d",
        })
        rows = [
            {"enabled": False, "date": "2026-02-15", "description": "x",
             "deposit": 0, "withdrawal": 100, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/csv-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="csv"
        ).count() == 0

    def test_no_category_skipped(self, db, logged_in_client, user, accounts):
        _setup_csv_session(logged_in_client)
        logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0", "desc_col": "1",
            "withdrawal_col": "2", "deposit_col": "3",
            "date_format": "%Y-%m-%d",
        })
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "x",
             "deposit": 0, "withdrawal": 100, "category_code": ""},
        ]
        resp = logged_in_client.post("/csv-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="csv"
        ).count() == 0

    def test_locked_period_skipped(self, db, logged_in_client, user, accounts):
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        _setup_csv_session(logged_in_client)
        logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0", "desc_col": "1",
            "withdrawal_col": "2", "deposit_col": "3",
            "date_format": "%Y-%m-%d",
        })
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "x",
             "deposit": 0, "withdrawal": 100, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/csv-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        # 確定済み期間なのでスキップ
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="csv"
        ).count() == 0

    def test_transfer_detection(self, db, logged_in_client, user, accounts):
        # category_code = 1020 (asset = 普通預金) なら振替仕訳
        _setup_csv_session(logged_in_client)
        logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0", "desc_col": "1",
            "withdrawal_col": "2", "deposit_col": "3",
            "date_format": "%Y-%m-%d",
        })
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "口座移動",
             "deposit": 0, "withdrawal": 5000, "category_code": "1020"},
        ]
        resp = logged_in_client.post("/csv-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        # 振替仕訳として 1件作成
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="csv"
        ).count() == 1


class TestReconcile:
    def test_unauthenticated(self, client):
        resp = client.post("/csv-import/reconcile")
        assert resp.status_code in (302, 401)

    def test_no_data(self, logged_in_client, accounts):
        resp = logged_in_client.post("/csv-import/reconcile")
        assert resp.status_code == 400

    def test_with_data(self, db, logged_in_client, user, accounts):
        _setup_csv_session(logged_in_client)
        logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0", "desc_col": "1",
            "withdrawal_col": "2", "deposit_col": "3",
            "date_format": "%Y-%m-%d",
        })
        with patch("app.services.reconciliation.find_matches") as mock_find:
            mock_find.return_value = {
                "csv_results": [], "journal_only": [], "daily_summary": [],
            }
            resp = logged_in_client.post("/csv-import/reconcile")
        assert resp.status_code == 200


class TestAiReconcile:
    """E2 PR-C-6c: 旧 POST /csv-import/ai-reconcile は廃止 (404)。
    新エンドポイント GET /csv-import/ai-reconcile-context は
    tests/test_csv_import_reconcile.py::TestAIReconcileContext でカバー。"""

    def test_unauthenticated_old_post_returns_404_or_redirect(self, client):
        # ルート自体が無くなったため認証チェック前に 404 が返る
        resp = client.post("/csv-import/ai-reconcile")
        assert resp.status_code == 404
