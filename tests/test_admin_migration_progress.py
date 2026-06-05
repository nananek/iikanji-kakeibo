"""E7 (#114) §16.6: /admin/migration-progress ダッシュボードのテスト。

Basic 認証 (環境変数) で保護され、Flask-Login セッションには依存しない。
集計は services/migration_status.py の共有関数を使う (CLI と同源)。
"""

import base64

import pytest

from app.models.user import User


def _user(db, name, *, public_key=None, temp_mk=None, active=True,
          user_type="personal"):
    u = User(username=name, email=f"{name}@e.com", user_type=user_type)
    u.set_password("pw")
    u.public_key = public_key
    u.migration_temp_mk = temp_mk
    u.is_active = active
    db.session.add(u)
    db.session.commit()
    return u


def _basic(user, pw):
    raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def ops_creds(app, monkeypatch):
    monkeypatch.setitem(app.config, "OPS_BASIC_AUTH_USER", "ops")
    monkeypatch.setitem(app.config, "OPS_BASIC_AUTH_PASS", "s3cret")
    return ("ops", "s3cret")


# --- 認証 ---


def test_503_when_creds_unset(client, app, monkeypatch):
    monkeypatch.setitem(app.config, "OPS_BASIC_AUTH_USER", "")
    monkeypatch.setitem(app.config, "OPS_BASIC_AUTH_PASS", "")
    resp = client.get("/admin/migration-progress.json")
    assert resp.status_code == 503


def test_401_without_auth_header(client, ops_creds):
    resp = client.get("/admin/migration-progress.json")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").startswith("Basic")


def test_401_with_wrong_password(client, ops_creds):
    resp = client.get("/admin/migration-progress.json",
                      headers=_basic("ops", "wrong"))
    assert resp.status_code == 401


def test_401_with_wrong_user(client, ops_creds):
    resp = client.get("/admin/migration-progress.json",
                      headers=_basic("attacker", "s3cret"))
    assert resp.status_code == 401


# --- JSON 内容 ---


def test_json_report_counts(client, db, ops_creds):
    # 鍵設定済み & temp-MK 保持 (移行待ち)
    _user(db, "a", public_key=b"k", temp_mk=bytes(32))
    # 鍵設定済み & temp-MK なし (完遂)
    _user(db, "b", public_key=b"k", temp_mk=None)
    # 鍵未設定 & ロック中 & temp-MK 保持
    _user(db, "c", public_key=None, temp_mk=bytes(32), active=False)
    # 監査アカウントは対象外
    _user(db, "aud", user_type="auditor", public_key=b"k")

    resp = client.get("/admin/migration-progress.json", headers=_basic(*ops_creds))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_users"] == 3
    assert data["users_with_keys"] == 2
    assert data["users_without_keys"] == 1
    assert data["users_locked"] == 1
    assert data["users_pending"] == 2
    assert data["data_re_encrypted_pct"] == round(2 / 3 * 100, 1)
    assert data["temp_mk_active"] is True
    assert data["temp_mk_finalize_eligible"] is False


def test_finalize_eligible_when_no_temp_mk(client, db, ops_creds):
    _user(db, "a", public_key=b"k", temp_mk=None)
    resp = client.get("/admin/migration-progress.json", headers=_basic(*ops_creds))
    data = resp.get_json()
    assert data["temp_mk_active"] is False
    assert data["temp_mk_finalize_eligible"] is True


def test_empty_db_pct_zero(client, db, ops_creds):
    resp = client.get("/admin/migration-progress.json", headers=_basic(*ops_creds))
    data = resp.get_json()
    assert data["total_users"] == 0
    assert data["data_re_encrypted_pct"] == 0.0


# --- HTML ---


def test_html_dashboard_renders(client, db, ops_creds):
    _user(db, "a", public_key=b"k", temp_mk=bytes(32))
    resp = client.get("/admin/migration-progress", headers=_basic(*ops_creds))
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert "E2EE 移行進捗" in resp.get_data(as_text=True)


# --- レート制限 (Basic 認証のブルートフォース耐性) ---


@pytest.fixture
def _restore_limiter():
    """レート制限テストが global limiter の enabled / storage を leak させ、
    後続テスト (login/register 等の低レート endpoint) を 429 にする既知の罠
    (feedback_test_limiter_leak) を防ぐ。テスト後に状態を復元する。
    """
    from app.extensions import limiter
    yield
    # セッション既定 (TestConfig.RATELIMIT_ENABLED=False) へ明示復元する。
    # fixture 解決順に依存して prev が True になる事故を避けるため固定値で戻す。
    limiter.enabled = False
    try:
        limiter.reset()
    except Exception:
        pass


def test_rate_limited_after_30_requests(ratelimit_app, ratelimit_client,
                                        monkeypatch, _restore_limiter):
    monkeypatch.setitem(ratelimit_app.config, "OPS_BASIC_AUTH_USER", "ops")
    monkeypatch.setitem(ratelimit_app.config, "OPS_BASIC_AUTH_PASS", "s3cret")
    # 認証失敗 (401) もレート制限の対象 (limiter が auth より外側)。
    for _ in range(30):
        r = ratelimit_client.get("/admin/migration-progress.json",
                                 headers=_basic("ops", "wrong"))
        assert r.status_code == 401
    # 31 回目 → 429
    r = ratelimit_client.get("/admin/migration-progress.json",
                             headers=_basic("ops", "wrong"))
    assert r.status_code == 429
