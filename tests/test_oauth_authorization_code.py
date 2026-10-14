"""MCP クライアント向け OAuth 2.1 (Authorization Code + PKCE) のテスト"""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from app.models.oauth import OAuthAuthorizationCode, OAuthToken

REDIRECT = "https://client.example.com/callback"


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _register(client, redirect=REDIRECT, method="none", name="Claude"):
    resp = client.post("/oauth/register", json={
        "client_name": name,
        "redirect_uris": [redirect],
        "token_endpoint_auth_method": method,
        "scope": "reports:read journals:read journals:create",
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()


def _authorize_post(logged_in_client, client_id, challenge, selected,
                    requested, redirect=REDIRECT, state="st", decision="approve"):
    return logged_in_client.post("/oauth/authorize", data={
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "requested_scope": requested,
        "scope": selected,
        "decision": decision,
    })


def _code_from(resp):
    loc = resp.headers["Location"]
    return parse_qs(urlparse(loc).query)["code"][0]


class TestDynamicClientRegistration:
    def test_register_returns_client_id(self, client):
        reg = _register(client)
        assert reg["client_id"].startswith("ikc_")
        assert reg["redirect_uris"] == [REDIRECT]
        assert reg["token_endpoint_auth_method"] == "none"
        assert "client_secret" not in reg

    def test_register_requires_redirect_uris(self, client):
        resp = client.post("/oauth/register", json={"client_name": "x"})
        assert resp.status_code == 400

    def test_register_rejects_insecure_redirect_uri(self, client):
        resp = client.post("/oauth/register", json={
            "redirect_uris": ["http://evil.example.com/cb"],
        })
        assert resp.status_code == 400

    def test_register_allows_localhost_http(self, client):
        resp = client.post("/oauth/register", json={
            "redirect_uris": ["http://localhost:8765/cb"],
        })
        assert resp.status_code == 201

    def test_confidential_client_gets_secret(self, client):
        reg = _register(client, method="client_secret_post")
        assert reg.get("client_secret")


class TestAuthorizationEndpoint:
    def test_requires_login(self, client):
        verifier, challenge = _pkce()
        reg = _register(client)
        resp = client.get("/oauth/authorize", query_string={
            "response_type": "code", "client_id": reg["client_id"],
            "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        assert resp.status_code in (302, 401)

    def test_logged_in_shows_consent(self, logged_in_client, client):
        verifier, challenge = _pkce()
        reg = _register(client)
        resp = logged_in_client.get("/oauth/authorize", query_string={
            "response_type": "code", "client_id": reg["client_id"],
            "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256", "scope": "reports:read",
        })
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "レポート" in body
        assert "承認" in body

    def test_unregistered_redirect_uri_rejected(self, logged_in_client, client):
        verifier, challenge = _pkce()
        reg = _register(client)
        resp = logged_in_client.get("/oauth/authorize", query_string={
            "response_type": "code", "client_id": reg["client_id"],
            "redirect_uri": "https://evil.example.com/cb",
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        assert resp.status_code == 400
        assert "redirect_uri" in resp.get_data(as_text=True)

    def test_missing_pkce_rejected(self, logged_in_client, client):
        reg = _register(client)
        resp = logged_in_client.get("/oauth/authorize", query_string={
            "response_type": "code", "client_id": reg["client_id"],
            "redirect_uri": REDIRECT,
        })
        assert resp.status_code == 400

    def test_no_scope_defaults_to_read_only(self, logged_in_client, client):
        """クライアントがスコープ未要求なら読み取り系のみを既定提示する。"""
        verifier, challenge = _pkce()
        reg = _register(client)
        resp = logged_in_client.get("/oauth/authorize", query_string={
            "response_type": "code", "client_id": reg["client_id"],
            "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "reports:read" in body
        # 書き込み系スコープは既定で提示しない (最小権限)
        assert "journals:create" not in body
        assert "journals:delete" not in body

    def test_deny_redirects_with_error(self, logged_in_client, client):
        verifier, challenge = _pkce()
        reg = _register(client)
        resp = _authorize_post(
            logged_in_client, reg["client_id"], challenge,
            selected=[], requested=["reports:read"], decision="deny",
        )
        assert resp.status_code in (302, 303)
        q = parse_qs(urlparse(resp.headers["Location"]).query)
        assert q["error"] == ["access_denied"]
        assert q["state"] == ["st"]


class TestAuthorizationCodeTokenExchange:
    def _flow(self, logged_in_client, client, selected, requested):
        verifier, challenge = _pkce()
        reg = _register(client)
        resp = _authorize_post(
            logged_in_client, reg["client_id"], challenge, selected, requested
        )
        code = _code_from(resp)
        return reg, verifier, code

    def _exchange(self, client, reg, verifier, code, redirect=REDIRECT):
        return client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": reg["client_id"],
            "redirect_uri": redirect,
            "code_verifier": verifier,
        })

    def test_full_flow_grants_requested_scopes(
        self, client, logged_in_client, db, user, accounts
    ):
        scopes = ["reports:read", "journals:read", "journals:create"]
        reg, verifier, code = self._flow(
            logged_in_client, client, scopes, scopes
        )
        resp = self._exchange(client, reg, verifier, code)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["access_token"].startswith("ikt_")
        assert body["refresh_token"].startswith("ikr_")
        assert set(body["scope"].split()) == set(scopes)

        # 発行トークンで REST API が利用できる
        api = client.get("/api/v1/journals", headers={
            "Authorization": f"Bearer {body['access_token']}"
        })
        assert api.status_code == 200

    def test_scope_narrowing_at_consent(
        self, client, logged_in_client, db, user, accounts
    ):
        requested = ["reports:read", "journals:read", "journals:create"]
        reg, verifier, code = self._flow(
            logged_in_client, client, ["reports:read"], requested
        )
        resp = self._exchange(client, reg, verifier, code)
        body = resp.get_json()
        assert body["scope"] == "reports:read"

        # 付与されなかったスコープは 403
        api = client.get("/api/v1/journals", headers={
            "Authorization": f"Bearer {body['access_token']}"
        })
        assert api.status_code == 403

    def test_read_only_when_only_read_scopes(
        self, client, logged_in_client, db, user, accounts
    ):
        requested = ["reports:read", "journals:create"]
        reg, verifier, code = self._flow(
            logged_in_client, client, ["reports:read"], requested
        )
        token_resp = self._exchange(client, reg, verifier, code)
        raw = token_resp.get_json()["access_token"]
        token = OAuthToken.query.filter_by(
            token_hash=OAuthToken.hash_token(raw)
        ).first()
        assert token.read_only is True

    def test_pkce_verifier_mismatch(self, client, logged_in_client, db, user):
        reg, verifier, code = self._flow(
            logged_in_client, client, ["reports:read"], ["reports:read"]
        )
        resp = self._exchange(client, reg, "wrong-verifier", code)
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_grant"

    def test_code_single_use(self, client, logged_in_client, db, user):
        reg, verifier, code = self._flow(
            logged_in_client, client, ["reports:read"], ["reports:read"]
        )
        assert self._exchange(client, reg, verifier, code).status_code == 200
        second = self._exchange(client, reg, verifier, code)
        assert second.status_code == 400
        assert second.get_json()["error"] == "invalid_grant"

    def test_expired_code(self, client, logged_in_client, db, user):
        reg, verifier, code = self._flow(
            logged_in_client, client, ["reports:read"], ["reports:read"]
        )
        rec = OAuthAuthorizationCode.query.first()
        rec.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db.session.commit()
        resp = self._exchange(client, reg, verifier, code)
        assert resp.status_code == 400

    def test_redirect_uri_mismatch(self, client, logged_in_client, db, user):
        reg, verifier, code = self._flow(
            logged_in_client, client, ["reports:read"], ["reports:read"]
        )
        resp = self._exchange(
            client, reg, verifier, code, redirect="https://other.example/cb"
        )
        assert resp.status_code == 400

    def test_unknown_client(self, client, logged_in_client, db, user):
        reg, verifier, code = self._flow(
            logged_in_client, client, ["reports:read"], ["reports:read"]
        )
        resp = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "ikc_unknown",
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        })
        assert resp.status_code == 401

    def test_confidential_client_requires_secret(
        self, client, logged_in_client, db, user
    ):
        verifier, challenge = _pkce()
        reg = _register(client, method="client_secret_post")
        resp = _authorize_post(
            logged_in_client, reg["client_id"], challenge,
            ["reports:read"], ["reports:read"],
        )
        code = _code_from(resp)
        # secret なしは 401
        assert self._exchange(client, reg, verifier, code).status_code == 401
        # secret ありは 200
        ok = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": reg["client_id"], "redirect_uri": REDIRECT,
            "code_verifier": verifier, "client_secret": reg["client_secret"],
        })
        assert ok.status_code == 200


class TestRefreshToken:
    def _issue(self, client, logged_in_client):
        verifier, challenge = _pkce()
        reg = _register(client)
        resp = _authorize_post(
            logged_in_client, reg["client_id"], challenge,
            ["reports:read"], ["reports:read"],
        )
        code = _code_from(resp)
        tok = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": reg["client_id"], "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        }).get_json()
        return reg, tok

    def test_refresh_rotates_and_works(
        self, client, logged_in_client, db, user, accounts
    ):
        reg, tok = self._issue(client, logged_in_client)
        resp = client.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": reg["client_id"],
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["access_token"].startswith("ikt_")
        assert body["refresh_token"] != tok["refresh_token"]
        assert body["scope"] == "reports:read"

        api = client.get("/api/v1/reports/trial-balance", headers={
            "Authorization": f"Bearer {body['access_token']}"
        })
        assert api.status_code == 200

        # 旧リフレッシュトークンはローテートで無効
        old = client.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": reg["client_id"],
        })
        assert old.status_code == 400

    def test_refresh_unknown_token(self, client, logged_in_client, db, user):
        reg, _ = self._issue(client, logged_in_client)
        resp = client.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": "ikr_unknown",
            "client_id": reg["client_id"],
        })
        assert resp.status_code == 400


class TestRevocation:
    def test_revoke_access_token(self, client, logged_in_client, db, user, accounts):
        verifier, challenge = _pkce()
        reg = _register(client)
        resp = _authorize_post(
            logged_in_client, reg["client_id"], challenge,
            ["reports:read"], ["reports:read"],
        )
        code = _code_from(resp)
        tok = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": reg["client_id"], "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        }).get_json()

        rev = client.post("/oauth/revoke", data={"token": tok["access_token"]})
        assert rev.status_code == 200

        api = client.get("/api/v1/journals", headers={
            "Authorization": f"Bearer {tok['access_token']}"
        })
        assert api.status_code == 401
