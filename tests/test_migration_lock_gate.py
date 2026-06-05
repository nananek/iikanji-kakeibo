"""E7 #114 PR-4b: 鍵未設定ロック (§16.5) のログインフロー + ゲートのテスト。

- ロック済 (is_active=False) ユーザーのログインは限定セッションを張り
  `/migration/locked` へ誘導される。
- ロック中ユーザーは鍵設定/退会に必要なエンドポイント以外をブロックされる。
- 鍵設定完了 (public_key 設定) で gate が自己回復しロックが解ける。
- 通常 (is_active=True) ユーザーは一切影響を受けない。

CLAUDE.md `feedback_flask_login_test_context` に従い 1 テスト 1 ログイン
(セッションは session_transaction で直接張る) で書く。
"""

from datetime import datetime, timezone

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
        assert sess.get("_user_id") == str(locked_user.id)


def test_login_active_user_goes_to_dashboard(client, user):
    """通常ユーザーのログインは従来どおりダッシュボードへ。"""
    resp = client.post(
        "/login",
        data={"username": "testuser", "password": "password123"},
    )
    assert resp.status_code == 302
    assert "/migration/locked" not in resp.headers["Location"]


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
        # ロック解決ページへのリダイレクトでないこと (= ゲートが許可)。
        if resp.status_code == 302:
            assert "/migration/locked" not in resp.headers["Location"]


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
