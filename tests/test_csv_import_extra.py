"""CSV取込ビュー (csv_import.py) の追加テスト

mapping → confirm → 取込実行までのフローと reconcile/ai-reconcile API。
"""

import io


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
    """confirm view は GET のみ (E3-F-5 で旧 POST 経路撤去)。取込実行は
    batch API 経由 (entries_builder + /api/v1/journals/batch) で行われ、
    そちらの挙動は tests/test_api.py / tests/static/js/ でテストする。"""

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


class TestReconcile:
    """E3-F PR-D-2: POST /csv-import/reconcile は削除済 (照合は client 側
    classical.findMatches に移植)。ルート自体が無いため認証前に 404。"""

    def test_post_reconcile_returns_404(self, client):
        resp = client.post("/csv-import/reconcile")
        assert resp.status_code == 404


class TestAiReconcile:
    """旧 POST /csv-import/ai-reconcile は廃止 (404)。
    新エンドポイント GET /csv-import/ai-reconcile-context は
    tests/test_csv_import_reconcile.py::TestAIReconcileContext でカバー。"""

    def test_unauthenticated_old_post_returns_404_or_redirect(self, client):
        # ルート自体が無くなったため認証チェック前に 404 が返る
        resp = client.post("/csv-import/ai-reconcile")
        assert resp.status_code == 404
