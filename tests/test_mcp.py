"""MCP (Model Context Protocol) リモートサーバー + OAuth ディスカバリのテスト"""

from datetime import datetime, timezone

from app.models.oauth import OAuthToken


def _make_token(db, user, scopes, read_only=False, kind_name="mcp-test"):
    raw, refresh, token = OAuthToken.issue(
        user_id=user.id, name=kind_name, scopes=scopes, read_only=read_only,
    )
    db.session.commit()
    return raw, refresh, token


def _rpc(client, token, method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", json=body, headers=headers)


class TestMcpAuth:
    def test_no_token_returns_401_with_www_authenticate(self, client):
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        assert resp.status_code == 401
        assert "resource_metadata=" in resp.headers.get("WWW-Authenticate", "")
        assert "/.well-known/oauth-protected-resource" in resp.headers[
            "WWW-Authenticate"
        ]

    def test_invalid_token_returns_401(self, client):
        resp = _rpc(client, "ikt_invalid", "initialize", {})
        assert resp.status_code == 401

    def test_api_key_accepted(self, client, auth_header, accounts):
        resp = client.post("/mcp", headers=auth_header, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        assert resp.status_code == 200
        assert resp.get_json()["result"]["protocolVersion"]


class TestMcpProtocol:
    def test_initialize_echoes_supported_version(self, client, db, user):
        raw, _, _ = _make_token(db, user, None)
        resp = _rpc(client, raw, "initialize", {"protocolVersion": "2025-03-26"})
        assert resp.status_code == 200
        result = resp.get_json()["result"]
        assert result["protocolVersion"] == "2025-03-26"
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"]

    def test_initialize_unknown_version_falls_back(self, client, db, user):
        raw, _, _ = _make_token(db, user, None)
        resp = _rpc(client, raw, "initialize", {"protocolVersion": "1999-01-01"})
        assert resp.get_json()["result"]["protocolVersion"] == "2025-06-18"

    def test_ping(self, client, db, user):
        raw, _, _ = _make_token(db, user, None)
        resp = _rpc(client, raw, "ping")
        assert resp.get_json()["result"] == {}

    def test_notification_returns_202_empty(self, client, db, user):
        raw, _, _ = _make_token(db, user, None)
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 202
        assert resp.get_data() == b""

    def test_unknown_method_returns_method_not_found(self, client, db, user):
        raw, _, _ = _make_token(db, user, None)
        resp = _rpc(client, raw, "does/not/exist")
        assert resp.get_json()["error"]["code"] == -32601

    def test_unknown_tool_returns_invalid_params(self, client, db, user):
        raw, _, _ = _make_token(db, user, None)
        resp = _rpc(client, raw, "tools/call", {"name": "nope", "arguments": {}})
        assert resp.get_json()["error"]["code"] == -32602

    def test_parse_error(self, client, db, user):
        raw, _, _ = _make_token(db, user, None)
        resp = client.post(
            "/mcp", data="{not json",
            headers={"Authorization": f"Bearer {raw}",
                     "Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_batch_request_rejected(self, client, db, user):
        raw, _, _ = _make_token(db, user, None)
        resp = client.post(
            "/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 400

    def test_get_mcp_returns_405(self, client):
        assert client.get("/mcp").status_code == 405


class TestMcpToolsList:
    def test_full_scope_lists_all_tools(self, client, db, user):
        raw, _, _ = _make_token(db, user, None)
        resp = _rpc(client, raw, "tools/list")
        names = [t["name"] for t in resp.get_json()["result"]["tools"]]
        assert "create_journal" in names
        assert "get_trial_balance" in names
        assert "delete_journal" in names

    def test_scoped_token_lists_only_permitted_tools(self, client, db, user):
        raw, _, _ = _make_token(db, user, ["reports:read"])
        resp = _rpc(client, raw, "tools/list")
        names = [t["name"] for t in resp.get_json()["result"]["tools"]]
        assert names == [
            "get_trial_balance", "get_income_statement",
            "get_monthly_comparison", "get_tax_summary",
        ]


class TestMcpToolsCall:
    def test_trial_balance(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["reports:read"])
        resp = _rpc(client, raw, "tools/call", {
            "name": "get_trial_balance", "arguments": {"year": 2026},
        })
        result = resp.get_json()["result"]
        assert result["isError"] is False
        assert "balances" in result["content"][0]["text"]

    def test_create_journal_with_scope(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["journals:create", "journals:read"])
        resp = _rpc(client, raw, "tools/call", {
            "name": "create_journal",
            "arguments": {
                "date": "2026-01-15",
                "description": "MCPテスト",
                "lines": [
                    {"account_code": "5010", "debit": 1000},
                    {"account_code": "1010", "credit": 1000},
                ],
            },
        })
        result = resp.get_json()["result"]
        assert result["isError"] is False
        assert "entry_number" in result["content"][0]["text"]

    def test_create_journal_denied_without_scope(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["reports:read"])
        resp = _rpc(client, raw, "tools/call", {
            "name": "create_journal",
            "arguments": {
                "date": "2026-01-15", "description": "x",
                "lines": [{"account_code": "5010", "debit": 1}],
            },
        })
        result = resp.get_json()["result"]
        assert result["isError"] is True
        assert "journals:create" in result["content"][0]["text"]

    def test_create_journal_denied_read_only_token(self, client, db, user, accounts):
        raw, _, _ = _make_token(
            db, user, ["journals:create"], read_only=True
        )
        resp = _rpc(client, raw, "tools/call", {
            "name": "create_journal",
            "arguments": {
                "date": "2026-01-15", "description": "x",
                "lines": [
                    {"account_code": "5010", "debit": 1},
                    {"account_code": "1010", "credit": 1},
                ],
            },
        })
        result = resp.get_json()["result"]
        assert result["isError"] is True
        assert "読み取り専用" in result["content"][0]["text"]

    def test_list_accounts(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["accounts:read"])
        resp = _rpc(client, raw, "tools/call", {"name": "list_accounts"})
        text = resp.get_json()["result"]["content"][0]["text"]
        assert "1010" in text

    def test_validation_error_is_tool_error(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["journals:create"])
        resp = _rpc(client, raw, "tools/call", {
            "name": "create_journal",
            "arguments": {"date": "2026-01-15", "description": "", "lines": []},
        })
        assert resp.get_json()["result"]["isError"] is True


class TestMcpScopeEnforcementOnRestApi:
    def test_limited_token_blocks_rest_scope(self, client, db, user, accounts):
        raw, _, _ = _make_token(db, user, ["reports:read"])
        resp = client.get(
            "/api/v1/journals", headers={"Authorization": f"Bearer {raw}"}
        )
        assert resp.status_code == 403

    def test_legacy_token_without_scopes_has_full_access(
        self, client, db, user, accounts
    ):
        raw, _, _ = _make_token(db, user, None)
        resp = client.get(
            "/api/v1/journals", headers={"Authorization": f"Bearer {raw}"}
        )
        assert resp.status_code == 200


class TestOAuthDiscovery:
    def test_protected_resource_metadata(self, client):
        resp = client.get("/.well-known/oauth-protected-resource")
        body = resp.get_json()
        assert body["resource"].endswith("/mcp")
        assert body["bearer_methods_supported"] == ["header"]
        assert "reports:read" in body["scopes_supported"]

    def test_protected_resource_metadata_path_variant(self, client):
        resp = client.get("/.well-known/oauth-protected-resource/mcp")
        assert resp.status_code == 200
        assert resp.get_json()["resource"].endswith("/mcp")

    def test_authorization_server_metadata(self, client):
        body = client.get("/.well-known/oauth-authorization-server").get_json()
        assert body["authorization_endpoint"].endswith("/oauth/authorize")
        assert body["registration_endpoint"].endswith("/oauth/register")
        assert body["code_challenge_methods_supported"] == ["S256"]
