"""E7 #114 PR-4b: 鍵未設定ロック (§16.5) のログインフロー + ゲートのテスト。

- ロック済 (is_active=False) ユーザーのログインは限定セッションを張り
  `/migration/locked` へ誘導される。
- ロック中ユーザーは鍵設定/退会に必要なエンドポイント以外をブロックされる。
- 鍵設定完了 (public_key 設定) で gate が自己回復しロックが解ける。
- 通常 (is_active=True) ユーザーは一切影響を受けない。

CLAUDE.md `feedback_flask_login_test_context` に従い 1 テスト 1 ログイン
(セッションは session_transaction で直接張る) で書く。
"""

import base64
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.user import User


@pytest.fixture
def locked_user(db):
    """鍵未設定ロック中のユーザー (is_active=False, public_key=None)。"""
    u = User(
        username="lockeduser",
        email="locked@example.com",
        user_type="personal",
        is_active=False,
        locked_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    return u


def _session_login(client, user_id):
    """Flask-Login のセッションを直接張る (force-login 済み状態の再現)。"""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)


# --- ログインフロー ---


def test_login_locked_user_redirects_to_locked(client, locked_user):
    """ロック済ユーザーのログインは /migration/locked へ 302。"""
    resp = client.post(
        "/login",
        data={"username": "lockeduser", "password": "password123"},
    )
    assert resp.status_code == 302
    assert "/migration/locked" in resp.headers["Location"]
    # 限定セッションが張られている (force-login)。
    with client.session_transaction() as sess:
        assert sess.get("_user_id") == f"{locked_user.id}.0"


def test_login_active_user_goes_to_dashboard(client, user):
    """通常ユーザーのログインは従来どおりダッシュボードへ。"""
    resp = client.post(
        "/login",
        data={"username": "testuser", "password": "password123"},
    )
    assert resp.status_code == 302
    assert "/migration/locked" not in resp.headers["Location"]


def test_recovery_login_locked_user_redirects_to_locked(client, db, locked_user):
    """ロック中ユーザーのリカバリログインは force-login で /migration/locked へ。

    passkey 専用ユーザーがロックされるとパスワードログインを使えないため、
    リカバリログインの force=True 対応が解除手段の生命線になる (デッドロック
    防止)。pending_recovery は設定しない (gate 相互リダイレクト回避)。
    """
    raw = locked_user.set_recovery_code()
    db.session.commit()
    resp = client.post(
        "/recovery",
        data={"username": "lockeduser", "recovery_code": raw},
    )
    assert resp.status_code == 302
    assert "/migration/locked" in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert sess.get("_user_id") == f"{locked_user.id}.0"
        # pending_recovery を立てない (lock gate とのループ回避)。
        assert sess.get("pending_recovery_action") is None


def test_passkey_login_locked_user_redirects_to_locked(client, db, locked_user):
    """passkey-only ロック済ユーザーの WebAuthn 認証は force-login + locked 誘導。

    passkey 専用ユーザーがロックされるとパスワードログインを使えないため、
    WebAuthn の force=True 対応がデッドロック防止の生命線になる。
    """
    from app.models.webauthn import WebAuthnCredential
    cred = WebAuthnCredential(
        user_id=locked_user.id,
        credential_id=b"\x11\x22\x33",
        credential_public_key=b"pub",
        current_sign_count=0,
        name="locked-passkey",
    )
    db.session.add(cred)
    db.session.commit()

    with client.session_transaction() as sess:
        sess["webauthn_auth_challenge"] = b"challenge"

    with patch("app.views.webauthn.verify_authentication_response") as mock_verify:
        v = MagicMock()
        v.new_sign_count = 1
        mock_verify.return_value = v
        raw_id = base64.urlsafe_b64encode(b"\x11\x22\x33").decode().rstrip("=")
        resp = client.post("/webauthn/authenticate/verify", json={"rawId": raw_id})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "/migration/locked" in body["redirect"]
    # force-login で限定セッションが張られている。
    with client.session_transaction() as sess:
        assert sess.get("_user_id") == f"{locked_user.id}.0"


# --- Bearer 認証のロック遮断 ---


def test_bearer_blocked_for_locked_user(client, db, locked_user):
    """ロック中ユーザーの API キーは Bearer 認証段階で 403 (§16.5)。"""
    from app.models.api_key import APIKey
    raw_key, key_hash, key_prefix = APIKey.generate()
    key = APIKey(
        user_id=locked_user.id,
        name="locked-key",
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes="journals:read",
        is_active=True,
    )
    db.session.add(key)
    db.session.commit()

    resp = client.get(
        "/api/v1/journals?year=2026",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 403


def test_oauth_token_blocked_for_locked_user(client, db, locked_user):
    """ロック中ユーザーの OAuth トークンも Bearer 認証段階で 403 (§16.5)。"""
    from app.models.oauth import OAuthToken
    raw, token_hash, prefix = OAuthToken.generate()
    tok = OAuthToken(
        user_id=locked_user.id,
        name="locked-oauth",
        token_hash=token_hash,
        token_prefix=prefix,
        is_active=True,
    )
    db.session.add(tok)
    db.session.commit()

    resp = client.get(
        "/api/v1/journals?year=2026",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 403


# --- ゲートのブロック ---


def test_locked_user_blocked_from_dashboard(client, locked_user):
    _session_login(client, locked_user.id)
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/migration/locked" in resp.headers["Location"]


def test_locked_page_accessible_while_locked(client, locked_user):
    _session_login(client, locked_user.id)
    resp = client.get("/migration/locked")
    assert resp.status_code == 200


def test_gate_allows_key_setup_apis(client, locked_user):
    """鍵設定/解錠に必要な wrapped-keys / keypair API はブロックしない。"""
    _session_login(client, locked_user.id)
    for url in ("/api/v1/wrapped-keys", "/api/v1/keypair"):
        resp = client.get(url)
        # ゲートによるロック解決ページへのリダイレクトでないこと (= 許可)。
        # 200 等で素通しされた場合もアサーションが評価されるよう、302 かつ
        # ロックページ宛て、という条件そのものを否定する。
        assert not (
            resp.status_code == 302
            and "/migration/locked" in resp.headers.get("Location", "")
        )


def test_gate_allows_delete_account(client, locked_user):
    _session_login(client, locked_user.id)
    resp = client.get("/settings/delete-account")
    assert resp.status_code == 200


# --- 自己回復 ---


def test_self_heal_on_public_key_set(client, db, locked_user):
    """public_key が立っていれば gate がロックを解除して通常通過させる。"""
    locked_user.public_key = b"x" * 32
    db.session.commit()
    _session_login(client, locked_user.id)

    resp = client.get("/")
    # ロック解決ページへ飛ばされない (= 解除された)。
    if resp.status_code == 302:
        assert "/migration/locked" not in resp.headers["Location"]

    refreshed = db.session.get(User, locked_user.id)
    assert refreshed.is_active is True
    assert refreshed.locked_at is None


# --- 非ロックユーザーは無影響 ---


def test_active_user_not_redirected(client, user):
    _session_login(client, user.id)
    resp = client.get("/")
    assert resp.status_code == 200


def test_locked_page_redirects_active_user(client, user):
    """ロックされていないユーザーが解決ページに来たらダッシュボードへ。"""
    _session_login(client, user.id)
    resp = client.get("/migration/locked")
    assert resp.status_code == 302
    assert "/migration/locked" not in resp.headers["Location"]
