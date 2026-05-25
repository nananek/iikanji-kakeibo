"""CSV取込 照合エンドポイントのテスト"""

from datetime import date
from unittest.mock import patch

import pytest

from tests.conftest import make_journal


def _set_session(client, data_key, payment_code):
    with client.session_transaction() as sess:
        sess["csv_data_key"] = data_key
        sess["csv_payment_account_code"] = payment_code


def _make_parsed_rows():
    return [
        {"row_num": 1, "date": "2026-01-10", "description": "コンビニ",
         "deposit": 0, "withdrawal": 1500},
        {"row_num": 2, "date": "2026-01-12", "description": "給与振込",
         "deposit": 200000, "withdrawal": 0},
    ]


class TestReconcile:
    """POST /csv-import/reconcile"""

    def test_no_session_data_returns_400(self, logged_in_client, accounts, account_types):
        resp = logged_in_client.post("/csv-import/reconcile")
        assert resp.status_code == 400

    def test_load_returns_none_gives_400(self, logged_in_client, accounts, account_types):
        _set_session(logged_in_client, "bad-key", "1020")
        with patch("app.views.csv_import.load_import_data", return_value=None):
            resp = logged_in_client.post("/csv-import/reconcile")
        assert resp.status_code == 400

    def test_success_returns_json_keys(self, db, user, logged_in_client, accounts,
                                       account_types):
        parsed = _make_parsed_rows()
        make_journal(db, user.id, "5010", "1020", 1500,
                     entry_date=date(2026, 1, 10), source="cashbook")
        _set_session(logged_in_client, "test-key", "1020")
        with patch("app.views.csv_import.load_import_data", return_value=parsed):
            resp = logged_in_client.post("/csv-import/reconcile")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "csv_results" in data
        assert "journal_only" in data
        assert "daily_summary" in data

    def test_matched_entry_in_results(self, db, user, logged_in_client, accounts,
                                      account_types):
        parsed = _make_parsed_rows()
        make_journal(db, user.id, "5010", "1020", 1500,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        _set_session(logged_in_client, "test-key", "1020")
        with patch("app.views.csv_import.load_import_data", return_value=parsed):
            resp = logged_in_client.post("/csv-import/reconcile")
        statuses = [r["status"] for r in resp.get_json()["csv_results"]]
        assert "matched" in statuses

    def test_empty_csv_rows(self, logged_in_client, accounts, account_types):
        """空リストは falsy なので 400 になる（ビューの not parsed チェック）"""
        _set_session(logged_in_client, "test-key", "1020")
        with patch("app.views.csv_import.load_import_data", return_value=[]):
            resp = logged_in_client.post("/csv-import/reconcile")
        assert resp.status_code == 400

    def test_unauthenticated_redirects(self, client, accounts, account_types):
        resp = client.post("/csv-import/reconcile")
        assert resp.status_code in (302, 401)


class TestAIReconcileContext:
    """GET /csv-import/ai-reconcile-context (新エンドポイント)。
    旧 POST /csv-import/ai-reconcile は廃止 (LLM 呼出は client-side)。"""

    def test_no_session_data_returns_400(
        self, logged_in_client, accounts, account_types,
    ):
        resp = logged_in_client.get("/csv-import/ai-reconcile-context")
        assert resp.status_code == 400

    def test_returns_context_with_unmatched_and_candidates(
        self, db, user, logged_in_client, accounts, account_types,
    ):
        parsed = self._setup_unmatched_and_journal_only(db, user)
        _set_session(logged_in_client, "test-key", "1020")
        with patch("app.views.csv_import.load_import_data", return_value=parsed):
            resp = logged_in_client.get("/csv-import/ai-reconcile-context")
        assert resp.status_code == 200
        body = resp.get_json()
        # プレースホルダ 2 種
        assert "__CSV_ROWS_TEXT__" in body["prompt_template"]
        assert "__JOURNAL_ROWS_TEXT__" in body["prompt_template"]
        # unmatched_csv + journal_candidates
        assert isinstance(body["unmatched_csv"], list)
        assert isinstance(body["journal_candidates"], list)
        assert body["batch_size"] == 30
        from app.services.ai_receipt import PROVIDER_DEFAULTS
        for k in ("openai", "anthropic", "google"):
            assert body["default_model_by_provider"][k] == PROVIDER_DEFAULTS[k]
        assert "llama_cpp" not in body["default_model_by_provider"]
        # api_key 一切返却しない
        assert "api_key_blob" not in body

    def _setup_unmatched_and_journal_only(self, db, user):
        parsed = [{"row_num": 1, "date": "2026-01-10", "description": "アマゾン",
                   "deposit": 0, "withdrawal": 3000}]
        make_journal(db, user.id, "5010", "1020", 2980,
                     entry_date=date(2026, 1, 10), source="ai_receipt")
        return parsed

    def test_unauthenticated_redirects(self, client, accounts, account_types):
        resp = client.get("/csv-import/ai-reconcile-context")
        assert resp.status_code in (302, 401)

    def test_old_post_endpoint_returns_404(
        self, logged_in_client, accounts, account_types,
    ):
        """旧 POST /csv-import/ai-reconcile は削除済 (404)。"""
        resp = logged_in_client.post("/csv-import/ai-reconcile")
        assert resp.status_code == 404
