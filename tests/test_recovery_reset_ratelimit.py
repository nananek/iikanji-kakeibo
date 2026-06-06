"""#385 PR-4b-2: /auth/recovery のレート制限テスト (設計書 §3.4.1 実装チェックリスト)。

begin : per-IP 5/min + per-username 10/hour
finish: per-IP 5/min + per-username 5/hour

per-username の独立検証には REMOTE_ADDR を毎回変えて per-IP 制限を回避する。
レート制限有効の専用 app (ratelimit_app/ratelimit_client) を使う。
"""

from base64 import b64encode

import pytest


@pytest.fixture(autouse=True)
def _login_secret(ratelimit_app):
    ratelimit_app.config["LOGIN_SERVER_SECRET"] = "test-login-server-secret"
    yield


def _b64(raw):
    return b64encode(raw).decode("ascii")


def _ip(i):
    return {"REMOTE_ADDR": f"10.0.0.{i}"}


def _finish_payload(username):
    return {
        "username": username,
        "recovery_verifier": _b64(b"\x11" * 32),
        "login_verifier": _b64(b"\x22" * 32),
        "login_salt": _b64(b"\x07" * 16),
        "login_kdf_params": {"memory": 65536, "iterations": 3, "parallelism": 1},
        "passphrase_wrapped_master_key": _b64(b"\x03" * 48),
        "passphrase_wrap_iv": _b64(b"\x04" * 12),
        "recovery_wrapped_master_key": _b64(b"\x05" * 48),
        "recovery_wrap_iv": _b64(b"\x06" * 12),
        "new_recovery_verifier": _b64(b"\x33" * 32),
    }


def test_begin_per_ip_rate_limit(ratelimit_client):
    """同一 IP から begin 6 回目で 429 (per-IP 5/min)。"""
    for _ in range(5):
        r = ratelimit_client.post("/auth/recovery/begin", json={"username": "nobody"})
        assert r.status_code == 200
    r = ratelimit_client.post("/auth/recovery/begin", json={"username": "nobody"})
    assert r.status_code == 429


def test_begin_per_username_rate_limit(ratelimit_client):
    """IP を変えても同一 username で begin 11 回目に 429 (per-username 10/hour)。"""
    for i in range(10):
        r = ratelimit_client.post(
            "/auth/recovery/begin", json={"username": "victim"},
            environ_overrides=_ip(i),
        )
        assert r.status_code == 200
    r = ratelimit_client.post(
        "/auth/recovery/begin", json={"username": "victim"},
        environ_overrides=_ip(99),
    )
    assert r.status_code == 429


def test_finish_per_username_rate_limit(ratelimit_client):
    """IP を変えても同一 username で finish 6 回目に 429 (per-username 5/hour、
    verifier 総当たり抑止)。"""
    for i in range(5):
        r = ratelimit_client.post(
            "/auth/recovery/finish", json=_finish_payload("victim"),
            environ_overrides=_ip(i),
        )
        # 未知ユーザーなので 401 だが、レート制限カウンタには計上される。
        assert r.status_code == 401
    r = ratelimit_client.post(
        "/auth/recovery/finish", json=_finish_payload("victim"),
        environ_overrides=_ip(99),
    )
    assert r.status_code == 429
