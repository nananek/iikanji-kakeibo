"""#385 PR-T2: TOTP 登録/確認/無効化/バックアップコード UI+API のテスト (§3.6)。"""

import pyotp
import pytest

from app.extensions import db as _db
from app.models.user import User
from app.models.totp_backup_code import TotpBackupCode
from app.services import login_derived as ld
from app.services import totp as totp_svc


LOGIN_SECRET = "test-login-server-secret"


@pytest.fixture(autouse=True)
def _login_secret(app):
    prev = app.config.get("LOGIN_SERVER_SECRET", "")
    app.config["LOGIN_SERVER_SECRET"] = LOGIN_SECRET
    yield
    app.config["LOGIN_SERVER_SECRET"] = prev


def _login(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _current_code(user, app):
    """user の保存済み secret から今の 6 桁コードを計算する。"""
    with app.app_context():
        u = _db.session.get(User, user.id)
        secret = ld.decrypt_totp_secret(
            u.totp_secret_encrypted, u.totp_secret_iv, u.id
        )
    return pyotp.TOTP(totp_svc.secret_to_base32(secret)).now()


# --- service 層 ---

def test_verify_code_roundtrip(app):
    with app.app_context():
        secret = totp_svc.generate_secret_bytes()
        code = pyotp.TOTP(totp_svc.secret_to_base32(secret)).now()
        assert totp_svc.verify_code(secret, code) is True
        assert totp_svc.verify_code(secret, "000000") is False
        assert totp_svc.verify_code(secret, "abc") is False


def test_generate_backup_codes(app, db, user):
    with app.app_context():
        codes = totp_svc.generate_backup_codes(user.id)
        db.session.commit()
        assert len(codes) == 10
        assert all(len(c) == 10 for c in codes)
        assert TotpBackupCode.query.filter_by(user_id=user.id).count() == 10
        # 再生成で旧コードは消える (常に 10 件)
        totp_svc.generate_backup_codes(user.id)
        db.session.commit()
        assert TotpBackupCode.query.filter_by(user_id=user.id).count() == 10


def test_consume_backup_code(app, db, user):
    with app.app_context():
        codes = totp_svc.generate_backup_codes(user.id)
        db.session.commit()
        assert totp_svc.consume_backup_code(user.id, codes[0]) is True
        db.session.commit()
        # 同じコードは 2 回使えない
        assert totp_svc.consume_backup_code(user.id, codes[0]) is False
        # 未知コードは False
        assert totp_svc.consume_backup_code(user.id, "deadbeef00") is False


# --- ルート (verify-before-enable) ---

def test_begin_stores_secret_not_enabled(client, db, user):
    _login(client, user)
    r = client.post("/settings/totp/begin")
    assert r.status_code == 302
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_secret_encrypted is not None
    assert refreshed.totp_enabled is False  # 確認前は無効


def test_totp_page_shows_qr_while_enrolling(client, db, user):
    _login(client, user)
    client.post("/settings/totp/begin")
    r = client.get("/settings/totp")
    assert r.status_code == 200
    assert b"<svg" in r.data           # QR が表示される
    assert "確認して有効化".encode() in r.data


def test_confirm_with_valid_code_enables_and_shows_backup(client, db, user, app):
    _login(client, user)
    client.post("/settings/totp/begin")
    code = _current_code(user, app)
    r = client.post("/settings/totp/confirm", data={"code": code})
    assert r.status_code == 200
    assert b"backup-codes" in r.data    # バックアップコード表示
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_enabled is True
    assert refreshed.totp_confirmed_at is not None
    assert TotpBackupCode.query.filter_by(user_id=user.id).count() == 10


def test_confirm_with_wrong_code_does_not_enable(client, db, user):
    _login(client, user)
    client.post("/settings/totp/begin")
    r = client.post("/settings/totp/confirm", data={"code": "000000"}, follow_redirects=False)
    assert r.status_code == 302
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_enabled is False


def test_disable_clears_everything(client, db, user, app):
    _login(client, user)
    client.post("/settings/totp/begin")
    client.post("/settings/totp/confirm", data={"code": _current_code(user, app)})
    r = client.post("/settings/totp/disable")
    assert r.status_code == 302
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_enabled is False
    assert refreshed.totp_secret_encrypted is None
    assert TotpBackupCode.query.filter_by(user_id=user.id).count() == 0


def test_regenerate_backup_codes_requires_enabled(client, db, user):
    _login(client, user)
    # TOTP 未有効では再生成不可
    r = client.post("/settings/totp/backup-codes/regenerate")
    assert r.status_code == 302  # flash + redirect
    assert TotpBackupCode.query.filter_by(user_id=user.id).count() == 0


def test_totp_404_when_not_configured(client, db, user, app):
    _login(client, user)
    app.config["LOGIN_SERVER_SECRET"] = ""
    assert client.get("/settings/totp").status_code == 404
    assert client.post("/settings/totp/begin").status_code == 404
