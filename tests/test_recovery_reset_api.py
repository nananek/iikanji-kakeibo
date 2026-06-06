"""#385 PR-4b-2: /auth/recovery/begin・finish のテスト。

設計書 docs/v5-e2ee/login-derived-mk.md §3.4.1。

verifier の派生 (HKDF) はサーバでは検証されず HMAC して照合するだけなので、テストでは
任意の 32B を verifier として用いる (Argon2id/HKDF の実値は不要)。
"""

import hashlib
import hmac
from base64 import b64decode, b64encode

import pytest

from app.extensions import db, limiter
from app.models.user import User, load_user
from app.models.wrapped_key import (
    METHOD_PASSPHRASE,
    METHOD_RECOVERY_SEED,
    WrappedKey,
)
from app.services import login_derived as ld


LOGIN_SECRET = "test-login-server-secret"


@pytest.fixture(autouse=True)
def _login_secret(app):
    prev = app.config.get("LOGIN_SERVER_SECRET", "")
    app.config["LOGIN_SERVER_SECRET"] = LOGIN_SECRET
    yield
    app.config["LOGIN_SERVER_SECRET"] = prev


@pytest.fixture(autouse=True)
def _reset_rate_limits(app):
    with app.app_context():
        try:
            limiter.reset()
        except Exception:
            pass
    yield


def _b64(raw):
    return b64encode(raw).decode("ascii")


def _verifier(byte):
    return bytes([byte]) * 32


def _expected_recovery_hash(verifier):
    return hmac.new(
        LOGIN_SECRET.encode(), b"recovery-hash\x00" + verifier, hashlib.sha256,
    ).digest()


def _seed_reset_user(db, username="resetuser", verifier_byte=0x11):
    """login 派生済み + recovery_seed wrapped_key + recovery_seed_server_hash を持つ
    リセット可能ユーザーを作る。"""
    u = User(username=username, email=f"{username}@example.com", user_type="personal")
    u.login_salt = b"\x01" * 16
    u.login_server_hash = ld.compute_login_server_hash(_verifier(0x99))
    u.login_kdf_params = {"memory": 65536, "iterations": 3, "parallelism": 1}
    u.login_secret_version = 1
    verifier = _verifier(verifier_byte)
    u.recovery_seed_server_hash = ld.compute_recovery_server_hash(verifier)
    db.session.add(u)
    db.session.flush()
    rec = WrappedKey(
        user_id=u.id,
        method=METHOD_RECOVERY_SEED,
        wrapped_master_key=b"\xaa" * 48,
        wrap_iv=b"\xbb" * 12,
        salt=None,
        kdf_params=None,
        label="リカバリシード",
    )
    db.session.add(rec)
    db.session.commit()
    return u, verifier


def _finish_payload(username, recovery_verifier, **overrides):
    payload = {
        "username": username,
        "recovery_verifier": _b64(recovery_verifier),
        "login_verifier": _b64(_verifier(0x22)),
        "login_salt": _b64(b"\x07" * 16),
        "login_kdf_params": {"memory": 65536, "iterations": 3, "parallelism": 1},
        "passphrase_wrapped_master_key": _b64(b"\x03" * 48),
        "passphrase_wrap_iv": _b64(b"\x04" * 12),
        "recovery_wrapped_master_key": _b64(b"\x05" * 48),
        "recovery_wrap_iv": _b64(b"\x06" * 12),
        "new_recovery_verifier": _b64(_verifier(0x33)),
    }
    payload.update(overrides)
    return payload


# --- begin -------------------------------------------------------------------

def test_begin_real_returns_recovery_wrapped_key(client, db):
    u, _ = _seed_reset_user(db)
    r = client.post("/auth/recovery/begin", json={"username": "resetuser"})
    assert r.status_code == 200
    body = r.get_json()
    assert b64decode(body["wrapped_master_key"]) == b"\xaa" * 48
    assert b64decode(body["wrap_iv"]) == b"\xbb" * 12


def test_begin_unknown_user_returns_dummy_same_length(client, db):
    r = client.post("/auth/recovery/begin", json={"username": "nobody"})
    assert r.status_code == 200
    body = r.get_json()
    assert len(b64decode(body["wrapped_master_key"])) == 48
    assert len(b64decode(body["wrap_iv"])) == 12


def test_begin_dummy_is_deterministic(client, db):
    a = client.post("/auth/recovery/begin", json={"username": "nobody"}).get_json()
    b = client.post("/auth/recovery/begin", json={"username": "nobody"}).get_json()
    assert a == b  # 同一 username で 2 回叩いても値が変わらない (列挙耐性)


def test_begin_null_hash_returns_dummy(client, db):
    """§3.4.1 移行期の非対称状態 (WARN-1) その1: hash 無 / wrapped_key 有 → ダミー応答。
    recovery_seed_server_hash が NULL (旧ウィザードで作成したユーザー) のケース。"""
    u = User(username="oldwiz", email="oldwiz@example.com", user_type="personal")
    u.login_salt = b"\x01" * 16
    db.session.add(u)
    db.session.flush()
    db.session.add(WrappedKey(
        user_id=u.id, method=METHOD_RECOVERY_SEED,
        wrapped_master_key=b"\xcc" * 48, wrap_iv=b"\xdd" * 12,
    ))
    db.session.commit()
    body = client.post("/auth/recovery/begin", json={"username": "oldwiz"}).get_json()
    # 実 wrapped_key (\xcc...) ではなくダミーが返る。
    assert b64decode(body["wrapped_master_key"]) != b"\xcc" * 48


def test_begin_hash_but_no_wrapped_key_returns_dummy(client, db):
    """§3.4.1 移行期の非対称状態 (WARN-1) その2: hash 有 / wrapped_key 無 → ダミー応答。"""
    u = User(username="nokey", email="nokey@example.com", user_type="personal")
    u.login_salt = b"\x01" * 16
    u.recovery_seed_server_hash = ld.compute_recovery_server_hash(_verifier(0x44))
    db.session.add(u)
    db.session.commit()
    r = client.post("/auth/recovery/begin", json={"username": "nokey"})
    assert r.status_code == 200
    assert len(b64decode(r.get_json()["wrapped_master_key"])) == 48


def test_begin_requires_username(client, db):
    assert client.post("/auth/recovery/begin", json={}).status_code == 400


def test_begin_not_configured_503(client, db, app):
    app.config["LOGIN_SERVER_SECRET"] = ""
    assert client.post(
        "/auth/recovery/begin", json={"username": "x"}
    ).status_code == 503


# --- finish ------------------------------------------------------------------

def test_finish_success_updates_all(client, db):
    u, verifier = _seed_reset_user(db)
    new_login_v = _verifier(0x22)
    new_rec_v = _verifier(0x33)
    r = client.post("/auth/recovery/finish", json=_finish_payload("resetuser", verifier))
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}

    refreshed = db.session.get(User, u.id)
    assert refreshed.login_server_hash == ld.compute_login_server_hash(new_login_v)
    assert refreshed.login_salt == b"\x07" * 16
    assert refreshed.recovery_seed_server_hash == _expected_recovery_hash(new_rec_v)
    assert refreshed.session_token_version == 1  # 失効カウンタ +1

    pp = WrappedKey.query.filter_by(user_id=u.id, method=METHOD_PASSPHRASE).first()
    assert pp.wrapped_master_key == b"\x03" * 48
    rec = WrappedKey.query.filter_by(user_id=u.id, method=METHOD_RECOVERY_SEED).first()
    assert rec.wrapped_master_key == b"\x05" * 48  # シードローテで差し替わった


def test_finish_clears_totp(client, db):
    """§3.6.5: リカバリリセットは TOTP をバイパス + 初期化する。secret/確認状態クリア +
    バックアップコード削除。"""
    from app.models.totp_backup_code import TotpBackupCode

    u, verifier = _seed_reset_user(db, username="totpuser", verifier_byte=0x66)
    u.totp_enabled = True
    u.totp_secret_encrypted = b"\xab" * 36
    u.totp_secret_iv = b"\xcd" * 12
    u.totp_last_used_step = 12345
    db.session.add(TotpBackupCode(user_id=u.id, code_hash="x" * 64, code_prefix="ab.."))
    db.session.commit()

    r = client.post("/auth/recovery/finish", json=_finish_payload("totpuser", verifier))
    assert r.status_code == 200
    refreshed = db.session.get(User, u.id)
    assert refreshed.totp_enabled is False
    assert refreshed.totp_secret_encrypted is None
    assert refreshed.totp_secret_iv is None
    assert refreshed.totp_last_used_step is None
    assert TotpBackupCode.query.filter_by(user_id=u.id).count() == 0


def test_finish_clears_passkey_only_login(client, db):
    """§3.4.1 passkey_only revival: リセット成功で passkey_only_login が解除され、
    設定した新パスワードでログインできるようになる (passkey 紛失時の詰み防止)。"""
    u, verifier = _seed_reset_user(db, username="pkonly", verifier_byte=0x55)
    u.passkey_only_login = True
    db.session.commit()
    r = client.post("/auth/recovery/finish", json=_finish_payload("pkonly", verifier))
    assert r.status_code == 200
    refreshed = db.session.get(User, u.id)
    assert refreshed.passkey_only_login is False
    assert refreshed.login_server_hash == ld.compute_login_server_hash(_verifier(0x22))


def test_begin_overlong_username_rejected(client, db):
    r = client.post("/auth/recovery/begin", json={"username": "a" * 256})
    assert r.status_code == 400


def test_finish_wrong_verifier_rejected(client, db):
    _seed_reset_user(db)
    r = client.post(
        "/auth/recovery/finish",
        json=_finish_payload("resetuser", _verifier(0xEE)),  # 誤った verifier
    )
    assert r.status_code == 401


def test_finish_unknown_user_rejected(client, db):
    r = client.post(
        "/auth/recovery/finish",
        json=_finish_payload("nobody", _verifier(0x11)),
    )
    assert r.status_code == 401


def test_finish_null_hash_rejected(client, db):
    """recovery_seed_server_hash が NULL のユーザーは finish できない。"""
    u = User(username="oldwiz2", email="oldwiz2@example.com", user_type="personal")
    u.login_salt = b"\x01" * 16
    db.session.add(u)
    db.session.commit()
    r = client.post(
        "/auth/recovery/finish",
        json=_finish_payload("oldwiz2", _verifier(0x11)),
    )
    assert r.status_code == 401


def test_finish_null_hash_runs_compare_digest(client, db, monkeypatch):
    """§3.4.1: finish が recovery_seed_server_hash=NULL でも compare_digest を必ず
    実行してから失敗する (早期 return せずタイミングでシード有無を漏らさない)。"""
    u = User(username="oldwiz3", email="oldwiz3@example.com", user_type="personal")
    u.login_salt = b"\x01" * 16
    db.session.add(u)
    db.session.commit()

    calls = {"n": 0}
    real = ld.hmac.compare_digest

    def _counting(a, b):
        calls["n"] += 1
        return real(a, b)

    monkeypatch.setattr(ld.hmac, "compare_digest", _counting)
    r = client.post(
        "/auth/recovery/finish",
        json=_finish_payload("oldwiz3", _verifier(0x11)),
    )
    assert r.status_code == 401
    assert calls["n"] >= 1  # NULL hash でも照合が走った


def test_finish_invalid_request_rejected(client, db):
    _seed_reset_user(db)
    bad = _finish_payload("resetuser", _verifier(0x11))
    bad["login_salt"] = _b64(b"\x07" * 15)  # 15B != 16B
    assert client.post("/auth/recovery/finish", json=bad).status_code == 400


def test_finish_weak_kdf_rejected(client, db):
    _seed_reset_user(db)
    bad = _finish_payload("resetuser", _verifier(0x11))
    bad["login_kdf_params"] = {"memory": 1024, "iterations": 1, "parallelism": 1}
    assert client.post("/auth/recovery/finish", json=bad).status_code == 400


def test_finish_seed_rotation_invalidates_old_verifier(client, db):
    """リセット後、旧シードの verifier では再リセットできず、新シードでのみ可能。"""
    u, old_verifier = _seed_reset_user(db)
    new_rec_v = _verifier(0x33)
    assert client.post(
        "/auth/recovery/finish", json=_finish_payload("resetuser", old_verifier)
    ).status_code == 200
    # 旧 verifier はもう通らない
    assert client.post(
        "/auth/recovery/finish", json=_finish_payload("resetuser", old_verifier)
    ).status_code == 401
    # 新 verifier なら通る (recovery_wrapped_master_key を再度差し替える)
    assert client.post(
        "/auth/recovery/finish", json=_finish_payload("resetuser", new_rec_v)
    ).status_code == 200


def test_finish_session_invalidation(app, client, db):
    """finish 成功で session_token_version が上がり、旧 Cookie (version 0) が失効する。"""
    u, verifier = _seed_reset_user(db)
    assert client.post(
        "/auth/recovery/finish", json=_finish_payload("resetuser", verifier)
    ).status_code == 200
    with app.app_context():
        # 旧 Cookie 相当 (version 無し = 0) は失効、新 version でのみロード可。
        assert load_user(str(u.id)) is None
        assert load_user(f"{u.id}.1") is not None


def test_finish_not_configured_503(client, db, app):
    _seed_reset_user(db)
    app.config["LOGIN_SERVER_SECRET"] = ""
    assert client.post(
        "/auth/recovery/finish", json=_finish_payload("resetuser", _verifier(0x11))
    ).status_code == 503


# --- 公開リセットページ (#385 PR-4b-3) -------------------------------------

def test_reset_page_renders_when_configured(client, db):
    r = client.get("/auth/recovery-reset")
    assert r.status_code == 200
    assert b"recovery-reset-form" in r.data


def test_reset_page_404_when_not_configured(client, db, app):
    app.config["LOGIN_SERVER_SECRET"] = ""
    assert client.get("/auth/recovery-reset").status_code == 404


def test_reset_page_redirects_when_authenticated(client, db, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    r = client.get("/auth/recovery-reset")
    assert r.status_code == 302
    assert "/login" not in r.headers["Location"]
