"""#385 PR-4b-1: リカバリシード verifier 基盤のテスト。

設計書 docs/v5-e2ee/login-derived-mk.md §3.4.1。

- login_derived.compute_recovery_server_hash / verify_recovery_verifier
  (NULL ハッシュでも定数時間照合してから失敗、早期 return 禁止)
- POST /api/v1/wrapped-keys が recovery_seed 作成時に recovery_verifier を受け取り
  users.recovery_seed_server_hash を確立する (後方互換: 未指定なら従来どおり)
- User.get_id / load_user の session_token_version 焼き込みと後方互換
"""

import hashlib
import hmac
from base64 import b64encode

import pytest

from app.models.user import User, load_user
from app.models.wrapped_key import METHOD_PASSPHRASE, METHOD_RECOVERY_SEED
from app.services import login_derived


LOGIN_SECRET = "test-login-server-secret"


@pytest.fixture(autouse=True)
def _login_secret(app):
    prev = app.config.get("LOGIN_SERVER_SECRET", "")
    app.config["LOGIN_SERVER_SECRET"] = LOGIN_SECRET
    yield
    app.config["LOGIN_SERVER_SECRET"] = prev


def _b64(raw):
    return b64encode(raw).decode("ascii")


def _verifier(byte=0xCC):
    return bytes([byte]) * 32


def _expected_recovery_hash(verifier):
    return hmac.new(
        LOGIN_SECRET.encode(), b"recovery-hash\x00" + verifier, hashlib.sha256,
    ).digest()


def _login(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# --- login_derived ヘルパー ---------------------------------------------------

def test_compute_recovery_server_hash_matches_label(app):
    with app.app_context():
        v = _verifier()
        assert login_derived.compute_recovery_server_hash(v) == _expected_recovery_hash(v)


def test_recovery_hash_domain_separated_from_login_hash(app):
    """同じ入力でも recovery-hash と login-hash でラベルが違うので別値になる。"""
    with app.app_context():
        v = _verifier()
        assert (
            login_derived.compute_recovery_server_hash(v)
            != login_derived.compute_login_server_hash(v)
        )


def test_verify_recovery_verifier_match(app):
    with app.app_context():
        v = _verifier()
        stored = login_derived.compute_recovery_server_hash(v)
        assert login_derived.verify_recovery_verifier(stored, v) is True


def test_verify_recovery_verifier_mismatch(app):
    with app.app_context():
        stored = login_derived.compute_recovery_server_hash(_verifier(0xAA))
        assert login_derived.verify_recovery_verifier(stored, _verifier(0xBB)) is False


def test_verify_recovery_verifier_null_runs_constant_time(app, monkeypatch):
    """stored_hash が NULL でも compare_digest を必ず 1 回呼んでから False を返す
    (早期 return 禁止 = タイミングでシード有無を漏らさない)。"""
    with app.app_context():
        calls = {"n": 0}
        real = hmac.compare_digest

        def _counting(a, b):
            calls["n"] += 1
            return real(a, b)

        monkeypatch.setattr(login_derived.hmac, "compare_digest", _counting)
        result = login_derived.verify_recovery_verifier(None, _verifier())
        assert result is False
        assert calls["n"] == 1


# --- POST /api/v1/wrapped-keys (recovery_seed + recovery_verifier) -----------

def _recovery_payload(verifier=None):
    body = {
        "method": METHOD_RECOVERY_SEED,
        "wrapped_master_key": _b64(b"\x01" * 48),
        "wrap_iv": _b64(b"\x02" * 12),
        "salt": None,
        "kdf_params": None,
    }
    if verifier is not None:
        body["recovery_verifier"] = _b64(verifier)
    return body


def test_create_recovery_seed_stores_server_hash(client, db, user):
    _login(client, user)
    v = _verifier()
    r = client.post("/api/v1/wrapped-keys", json=_recovery_payload(v))
    assert r.status_code == 201
    refreshed = db.session.get(User, user.id)
    assert refreshed.recovery_seed_server_hash == _expected_recovery_hash(v)


def test_create_recovery_seed_without_verifier_is_backward_compatible(client, db, user):
    """recovery_verifier 未指定 (旧クライアント) でも作成でき、ハッシュは未設定のまま。"""
    _login(client, user)
    r = client.post("/api/v1/wrapped-keys", json=_recovery_payload(None))
    assert r.status_code == 201
    refreshed = db.session.get(User, user.id)
    assert refreshed.recovery_seed_server_hash is None


def test_recovery_verifier_skipped_when_secret_unset(app, client, db, user):
    """LOGIN_SERVER_SECRET 未設定 (login-derived 未活性化) でも recovery seed 作成は
    壊さず、ハッシュ保存だけスキップする (503 にしない = 回帰防止)。"""
    _login(client, user)
    app.config["LOGIN_SERVER_SECRET"] = ""
    r = client.post("/api/v1/wrapped-keys", json=_recovery_payload(_verifier()))
    assert r.status_code == 201
    refreshed = db.session.get(User, user.id)
    assert refreshed.recovery_seed_server_hash is None


def test_recovery_verifier_wrong_length_rejected(client, db, user):
    _login(client, user)
    body = _recovery_payload()
    body["recovery_verifier"] = _b64(b"\x01" * 31)  # 31B != 32B
    r = client.post("/api/v1/wrapped-keys", json=body)
    assert r.status_code == 400


def test_recovery_verifier_rejected_for_non_recovery_method(client, db, user):
    _login(client, user)
    body = {
        "method": METHOD_PASSPHRASE,
        "wrapped_master_key": _b64(b"\x01" * 48),
        "wrap_iv": _b64(b"\x02" * 12),
        "salt": _b64(b"\x03" * 16),
        "kdf_params": {"memory": 65536, "iterations": 3, "parallelism": 1},
        "recovery_verifier": _b64(_verifier()),
    }
    r = client.post("/api/v1/wrapped-keys", json=body)
    assert r.status_code == 400


# --- get_id / load_user の session_token_version 焼き込み + 後方互換 ----------

def test_get_id_includes_session_token_version(db, user):
    assert user.get_id() == f"{user.id}.0"
    user.bump_session_token_version()
    assert user.get_id() == f"{user.id}.1"


def test_load_user_legacy_cookie_without_version(app, db, user):
    """version 無し Cookie (旧形式) は version=0 として扱い、version 0 ユーザーは通過。"""
    with app.app_context():
        assert load_user(str(user.id)) is not None
        assert load_user(f"{user.id}.0") is not None


def test_load_user_version_mismatch_rejected(app, db, user):
    """DB の version を上げると旧 version の Cookie は失効 (None)。"""
    user.session_token_version = 1
    db.session.commit()
    with app.app_context():
        assert load_user(str(user.id)) is None       # 旧 Cookie (=>0) は失効
        assert load_user(f"{user.id}.0") is None
        assert load_user(f"{user.id}.1") is not None  # 新 Cookie は通過


def test_load_user_malformed_id_returns_none(app, db, user):
    with app.app_context():
        assert load_user("not-an-int") is None
        assert load_user(f"{user.id}.x") is None
