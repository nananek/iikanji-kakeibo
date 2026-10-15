"""MCP の CSV 照合・取込ツール (reconcile_csv / import_csv / preview_csv) のテスト。

クレカ明細 CSV を、フロントエンドの取込確認画面と同じロジックで照合し、
未照合行を仕訳化できることを確認する。
"""

from datetime import date

from tests.conftest import make_journal


def _make_token(db, user, scopes, read_only=False):
    from app.models.oauth import OAuthToken

    raw, refresh, token = OAuthToken.issue(
        user_id=user.id, name="csv-test", scopes=scopes, read_only=read_only,
    )
    db.session.commit()
    return raw, refresh, token


def _rpc(client, token, method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/mcp", json=body, headers=headers)


def _call(client, token, name, arguments):
    return _rpc(client, token, "tools/call",
                {"name": name, "arguments": arguments})


def _parse_tool_result(resp):
    import json
    result = resp.get_json()["result"]
    return json.loads(result["content"][0]["text"])


CC_CSV = (
    "利用日,利用店舗,利用金額\n"
    "2026-01-10,コンビニ,1500\n"
    "2026-01-12,スーパー,2000\n"
)

UNKNOWN_HEADER_CSV = (
    "colA,colB,colC\n"
    "2026-01-10,コンビニ,1500\n"
)


class TestPreviewCsv:
    def test_returns_headers_and_suggestion(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["journals:read"])
        resp = _call(client, raw, "preview_csv", {"content": CC_CSV})
        data = _parse_tool_result(resp)
        assert data["encoding"] in ("utf-8", "utf-8-sig")
        assert data["headers"] == ["利用日", "利用店舗", "利用金額"]
        assert data["total_rows"] == 2
        assert data["suggested_mapping"]["date_col"] == 0
        assert data["suggested_mapping"]["desc_col"] == 1
        assert data["suggested_mapping"]["withdrawal_col"] == 2

    def test_denied_without_scope(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["reports:read"])
        resp = _call(client, raw, "preview_csv", {"content": CC_CSV})
        result = resp.get_json()["result"]
        assert result["isError"] is True


class TestReconcileCsv:
    def test_matched_unmatched_and_receipt_journal(
        self, client, db, user, accounts
    ):
        # CSV 1/10 に一致する仕訳
        make_journal(db, user.id, "5010", "2010", 1500,
                     entry_date=date(2026, 1, 10), description="コンビニ")
        # CSV に無い仕訳 (レシート起票済み・明細未到着)
        make_journal(db, user.id, "5010", "2010", 999,
                     entry_date=date(2026, 1, 11), description="未達レシート")

        raw, _, _ = _make_token(db, user, ["journals:read"])
        resp = _call(client, raw, "reconcile_csv", {
            "content": CC_CSV, "payment_account_code": "2010",
        })
        data = _parse_tool_result(resp)

        assert data["needs_mapping"] is False
        assert data["mapping_source"] in ("saved", "heuristic")
        statuses = [r["status"] for r in data["csv_results"]]
        assert statuses[0] == "matched"
        assert statuses[1] == "unmatched"
        # 行データが同梱され、そのまま import_csv に渡せる
        assert data["csv_results"][1]["row"]["withdrawal"] == 2000

        # カード会社未達 (journal_only)
        assert any(j["description"] == "未達レシート"
                   for j in data["journal_only"])

        # 日計差
        by_date = {d["date"]: d for d in data["daily_summary"]}
        assert by_date["2026-01-10"]["diff_amount"] == 0
        assert by_date["2026-01-12"]["diff_amount"] == 2000
        assert by_date["2026-01-12"]["has_discrepancy"] is True

        s = data["summary"]
        assert s["matched_count"] == 1
        assert s["unmatched_count"] == 1
        assert s["journal_only_count"] == 1
        assert s["journal_only_total"] == 999

    def test_needs_mapping_when_headers_unknown(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["journals:read"])
        resp = _call(client, raw, "reconcile_csv", {
            "content": UNKNOWN_HEADER_CSV, "payment_account_code": "2010",
        })
        data = _parse_tool_result(resp)
        assert data["needs_mapping"] is True
        assert data["headers"] == ["colA", "colB", "colC"]

    def test_explicit_mapping(self, client, db, user, accounts):
        make_journal(db, user.id, "5010", "2010", 1500,
                     entry_date=date(2026, 1, 10), description="コンビニ")
        raw, _, _ = _make_token(db, user, ["journals:read"])
        resp = _call(client, raw, "reconcile_csv", {
            "content": UNKNOWN_HEADER_CSV,
            "payment_account_code": "2010",
            "mapping": {"date_col": 0, "desc_col": 1, "withdrawal_col": 2,
                        "date_format": "%Y-%m-%d"},
        })
        data = _parse_tool_result(resp)
        assert data["needs_mapping"] is False
        assert data["mapping_source"] == "explicit"
        assert data["csv_results"][0]["status"] == "matched"

    def test_unknown_account_is_tool_error(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["journals:read"])
        resp = _call(client, raw, "reconcile_csv", {
            "content": CC_CSV, "payment_account_code": "9999",
        })
        assert resp.get_json()["result"]["isError"] is True

    def test_missing_content_is_tool_error(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["journals:read"])
        resp = _call(client, raw, "reconcile_csv", {
            "payment_account_code": "2010",
        })
        assert resp.get_json()["result"]["isError"] is True

    def test_base64_content(self, client, db, user, accounts):
        import base64
        raw, _, _ = _make_token(db, user, ["journals:read"])
        b64 = base64.b64encode(CC_CSV.encode("utf-8")).decode("ascii")
        resp = _call(client, raw, "reconcile_csv", {
            "content_base64": b64, "payment_account_code": "2010",
        })
        data = _parse_tool_result(resp)
        assert data["parsed_count"] == 2

    def test_denied_without_scope(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["accounts:read"])
        resp = _call(client, raw, "reconcile_csv", {
            "content": CC_CSV, "payment_account_code": "2010",
        })
        assert resp.get_json()["result"]["isError"] is True


class TestImportCsv:
    def test_import_unmatched_row(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["journals:create", "journals:read"])
        resp = _call(client, raw, "import_csv", {
            "payment_account_code": "2010",
            "rows": [{
                "date": "2026-01-12", "description": "スーパー",
                "withdrawal": 2000, "category_code": "5010",
            }],
        })
        data = _parse_tool_result(resp)
        assert data["imported"] == 1
        assert data["skipped"] == 0

        # 起票された仕訳を確認
        listed = _parse_tool_result(_call(client, raw, "list_journals", {}))
        descs = [j["description"] for j in listed["journals"]]
        assert "スーパー" in descs

    def test_import_transfer_row(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["journals:create", "journals:read"])
        resp = _call(client, raw, "import_csv", {
            "payment_account_code": "2010",
            "rows": [{
                "date": "2026-01-12", "description": "口座振替",
                "withdrawal": 5000, "category_code": "1020",
            }],
        })
        data = _parse_tool_result(resp)
        assert data["imported"] == 1

    def test_missing_category_is_skipped(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["journals:create"])
        resp = _call(client, raw, "import_csv", {
            "payment_account_code": "2010",
            "rows": [{"date": "2026-01-12", "withdrawal": 2000}],
        })
        data = _parse_tool_result(resp)
        assert data["imported"] == 0
        assert data["skipped"] == 1

    def test_denied_without_create_scope(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["journals:read"])
        resp = _call(client, raw, "import_csv", {
            "payment_account_code": "2010",
            "rows": [{"date": "2026-01-12", "withdrawal": 1,
                      "category_code": "5010"}],
        })
        assert resp.get_json()["result"]["isError"] is True

    def test_denied_read_only_token(self, client, db, user, accounts):
        raw, _, _ = _make_token(
            db, user, ["journals:create"], read_only=True
        )
        resp = _call(client, raw, "import_csv", {
            "payment_account_code": "2010",
            "rows": [{"date": "2026-01-12", "withdrawal": 1,
                      "category_code": "5010"}],
        })
        result = resp.get_json()["result"]
        assert result["isError"] is True
        assert "読み取り専用" in result["content"][0]["text"]


class TestHeuristicMapping:
    def test_bank_headers_with_deposit_and_withdrawal(
        self, client, db, user, accounts
    ):
        csv_text = "取引日,相手先,出金額,入金額\n2026-02-01,店,1000,0\n"
        raw, _, _ = _make_token(db, user, ["journals:read"])
        data = _parse_tool_result(
            _call(client, raw, "preview_csv", {"content": csv_text})
        )
        m = data["suggested_mapping"]
        assert m["date_col"] == 0
        assert m["desc_col"] == 1
        assert m["withdrawal_col"] == 2
        assert m["deposit_col"] == 3


class TestToolsListIncludesCsvTools:
    def test_full_scope_lists_csv_tools(self, client, db, user):
        raw, _, _ = _make_token(db, user, None)
        names = [
            t["name"]
            for t in _rpc(client, raw, "tools/list").get_json()["result"]["tools"]
        ]
        assert "preview_csv" in names
        assert "reconcile_csv" in names
        assert "import_csv" in names

    def test_journals_read_only_lists_read_csv_tools(self, client, db, user):
        raw, _, _ = _make_token(db, user, ["journals:read"])
        names = [
            t["name"]
            for t in _rpc(client, raw, "tools/list").get_json()["result"]["tools"]
        ]
        assert "reconcile_csv" in names
        assert "import_csv" not in names
