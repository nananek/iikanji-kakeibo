"""#385 PR-T3: TOTP 2FA のログイン統合テスト (設計書 §3.6.4)。

begin が totp_required を返し、finish で login_verifier 照合後に TOTP / バックアップコードを
検証する。replay (同一 step 再利用) は拒否する。
"""

import hashlib
import hmac
from base64 import b64encode

import pyotp
import pytest

from app.models.user import User
from app.models.totp_backup_code import TotpBackupCode
from app.services import login_derived as ld
from app.services import totp as totp_svc


LOGIN_SECRET = "test-login-server-secret"
TOTP_SECRET = b"\x07" * 20


@pytest.fixture(autouse=True)
def _login_secret(app):
    prev = app.config.get("LOGIN_SERVER_SECRET", "")
    app.config["LOGIN_SERVER_SECRET"] = LOGIN_SECRET
    yield
    app.config["LOGIN_SERVER_SECRET"] = prev


def _b64(raw):
    return b64encode(raw).decode("ascii")


def _verifier(byte=0xAA):
    return bytes([byte]) * 32


def _totp_user(db, username="totplogin"):
    """login 派生済み + TOTP 有効ユーザーを作る。"""
    u = User(username=username, email=f"{username}@test.local", user_type="personal")
    u.login_salt = b"\x01" * 16
    u.login_server_hash = ld.compute_login_server_hash(_verifier())
    u.login_kdf_params = {"memory": 65536, "iterations": 3, "parallelism": 1}
    u.login_secret_version = 1
    u.totp_enabled = True
    db.session.add(u)
    db.session.flush()
    ct, iv = ld.encrypt_totp_secret(TOTP_SECRET, u.id)
    u.totp_secret_encrypted = ct
    u.totp_secret_iv = iv
    db.session.commit()
    return u


def _now_code():
    return pyotp.TOTP(totp_svc.secret_to_base32(TOTP_SECRET)).now()


def _begin(client, username="totplogin"):
    return client.post("/auth/login/begin", json={"username": username})


def _finish(client, username="totplogin", verifier=None, **extra):
    payload = {"username": username, "login_verifier": _b64(verifier or _verifier())}
    payload.update(extra)
    return client.post("/auth/login/finish", json=payload)


def test_begin_reports_totp_required(client, db):
    _totp_user(db)
    body = _begin(client).get_json()
    assert body["migration_required"] is False
    assert body["totp_required"] is True


def test_begin_totp_required_false_for_non_totp_user(client, db, user):
    # user fixture: werkzeug のみ (未移行)。totp_required は出ない/false。
    body = _begin(client, "testuser").get_json()
    assert body.get("totp_required", False) is False


def test_finish_without_totp_code_rejected(client, db):
    _totp_user(db)
    resp = _finish(client)  # totp_code 無し
    assert resp.status_code == 401


def test_finish_with_valid_totp_succeeds(client, db):
    _totp_user(db)
    resp = _finish(client, totp_code=_now_code(), totp_type="totp")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_finish_with_wrong_totp_rejected(client, db):
    _totp_user(db)
    resp = _finish(client, totp_code="000000", totp_type="totp")
    assert resp.status_code == 401


def test_finish_wrong_password_rejected_before_totp(client, db):
    """login_verifier が誤りなら TOTP 以前に 401 (照合順序)。"""
    _totp_user(db)
    resp = _finish(client, verifier=_verifier(0xBB), totp_code=_now_code())
    assert resp.status_code == 401


def test_finish_with_backup_code(client, db):
    u = _totp_user(db)
    raw = "abcd1234ef"
    db.session.add(TotpBackupCode(
        user_id=u.id, code_hash=hashlib.sha256(raw.encode()).hexdigest(),
        code_prefix="abcd...",
    ))
    db.session.commit()
    resp = _finish(client, totp_code=raw, totp_type="backup")
    assert resp.status_code == 200
    # 1 回限り: 消費済みになっている
    row = TotpBackupCode.query.filter_by(user_id=u.id).first()
    assert row.used_at is not None


def test_finish_backup_code_single_use(client, db):
    u = _totp_user(db)
    raw = "00ff00ff00"
    db.session.add(TotpBackupCode(
        user_id=u.id, code_hash=hashlib.sha256(raw.encode()).hexdigest(),
    ))
    db.session.commit()
    assert _finish(client, totp_code=raw, totp_type="backup").status_code == 200
    client.get("/logout")
    # 2 回目は使えない
    assert _finish(client, totp_code=raw, totp_type="backup").status_code == 401


def test_finish_totp_replay_rejected(client, db):
    """同一 step の TOTP コードは 2 回使えない (replay 対策、§3.6.4)。"""
    u = _totp_user(db)
    code = _now_code()
    assert _finish(client, totp_code=code, totp_type="totp").status_code == 200
    # totp_last_used_step が記録されている
    refreshed = db.session.get(User, u.id)
    assert refreshed.totp_last_used_step is not None
    client.get("/logout")
    # 同じ code (同じ step) は replay として拒否
    assert _finish(client, totp_code=code, totp_type="totp").status_code == 401


# --- service: verify_code_with_step ---

def test_verify_code_with_step_returns_step(app):
    with app.app_context():
        code = pyotp.TOTP(totp_svc.secret_to_base32(TOTP_SECRET)).now()
        ok, step = totp_svc.verify_code_with_step(TOTP_SECRET, code)
        assert ok is True and isinstance(step, int)
        # 同一 step を last_used に渡すと replay 判定
        ok2, _ = totp_svc.verify_code_with_step(TOTP_SECRET, code, last_used_step=step)
        assert ok2 is False
