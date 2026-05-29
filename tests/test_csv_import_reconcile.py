"""CSV取込 照合エンドポイントのテスト

E3-F PR-D-2: 決定論的マッチング (旧 POST /csv-import/reconcile) と日付スナップ
(旧 POST /csv-import/match/snap-date) はクライアント側 (classical.js /
PUT /api/v1/journals/<id>) に移植され、サーバルートは削除済。
GET /csv-import/ai-reconcile-context は平文を読まずプロンプト材料のみ返す。
等価のマッチングロジックは tests/static/js/test_reconcile_classical.mjs、
AI 照合は tests/static/js/test_reconcile_orchestrator.mjs がカバーする。
"""


class TestReconcileEndpointRemoved:
    """旧 POST /csv-import/reconcile は削除済 (404)。"""

    def test_post_reconcile_returns_404(self, logged_in_client, accounts, account_types):
        resp = logged_in_client.post("/csv-import/reconcile")
        assert resp.status_code == 404


class TestSnapDateEndpointRemoved:
    """旧 POST /csv-import/match/snap-date は削除済 (404)。"""

    def test_post_snap_date_returns_404(self, logged_in_client, accounts, account_types):
        resp = logged_in_client.post(
            "/csv-import/match/snap-date",
            json={"entry_id": 1, "csv_date": "2026-05-01"},
        )
        assert resp.status_code == 404


class TestAIReconcileContext:
    """GET /csv-import/ai-reconcile-context (プロンプト材料のみ)。

    旧 POST /csv-import/ai-reconcile は廃止 (LLM 呼出は client-side)。
    PR-D-2 で unmatched_csv / journal_candidates の返却を撤去 (平文を読まない)。
    """

    def test_returns_prompt_material_without_session(
        self, logged_in_client, accounts, account_types,
    ):
        # session に CSV データが無くても 200 (平文 / session を参照しない)。
        resp = logged_in_client.get("/csv-import/ai-reconcile-context")
        assert resp.status_code == 200
        body = resp.get_json()
        # プレースホルダ 2 種
        assert "__CSV_ROWS_TEXT__" in body["prompt_template"]
        assert "__JOURNAL_ROWS_TEXT__" in body["prompt_template"]
        assert body["batch_size"] == 30
        from app.services.ai_receipt import PROVIDER_DEFAULTS
        for k in ("openai", "anthropic", "google"):
            assert body["default_model_by_provider"][k] == PROVIDER_DEFAULTS[k]
        assert "llama_cpp" not in body["default_model_by_provider"]
        # PR-D-2: 平文由来のフィールドは返さない
        assert "unmatched_csv" not in body
        assert "journal_candidates" not in body
        # api_key 一切返却しない
        assert "api_key_blob" not in body
        assert "custom_prompt" in body

    def test_unauthenticated_redirects(self, client, accounts, account_types):
        resp = client.get("/csv-import/ai-reconcile-context")
        assert resp.status_code in (302, 401)

    def test_old_post_endpoint_returns_404(
        self, logged_in_client, accounts, account_types,
    ):
        """旧 POST /csv-import/ai-reconcile は削除済 (404)。"""
        resp = logged_in_client.post("/csv-import/ai-reconcile")
        assert resp.status_code == 404
