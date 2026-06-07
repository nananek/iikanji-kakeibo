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
    # 有効な TOTP の無効化はパスワード再認証が必要 (user fixture のパスワード)
    r = client.post("/settings/totp/disable", data={"password": "password123"})
    assert r.status_code == 302
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_enabled is False
    assert refreshed.totp_secret_encrypted is None
    assert TotpBackupCode.query.filter_by(user_id=user.id).count() == 0


def test_disable_requires_password_when_enabled(client, db, user, app):
    """有効な TOTP の無効化はパスワード無し/誤りでは行えない (再認証必須)。"""
    _login(client, user)
    client.post("/settings/totp/begin")
    client.post("/settings/totp/confirm", data={"code": _current_code(user, app)})
    # パスワード無し
    client.post("/settings/totp/disable", data={})
    assert db.session.get(User, user.id).totp_enabled is True
    # 誤パスワード
    client.post("/settings/totp/disable", data={"password": "wrongpass"})
    assert db.session.get(User, user.id).totp_enabled is True


def test_cancel_enrollment_without_password(client, db, user):
    """未確認の登録中止は降格でないのでパスワード不要 (totp_enabled=False のまま)。"""
    _login(client, user)
    client.post("/settings/totp/begin")
    r = client.post("/settings/totp/disable", data={})  # password 不要
    assert r.status_code == 302
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_secret_encrypted is None
    assert refreshed.totp_enabled is False


def test_regenerate_backup_codes_requires_enabled(client, db, user):
    _login(client, user)
    # TOTP 未有効では再生成不可
    r = client.post("/settings/totp/backup-codes/regenerate")
    assert r.status_code == 302  # flash + redirect
    assert TotpBackupCode.query.filter_by(user_id=user.id).count() == 0


def test_regenerate_backup_codes_happy_path(client, db, user, app):
    """TOTP 有効時、再生成で新 10 個が表示され旧コードは差し替わる。"""
    _login(client, user)
    client.post("/settings/totp/begin")
    client.post("/settings/totp/confirm", data={"code": _current_code(user, app)})
    first = TotpBackupCode.query.filter_by(user_id=user.id).all()
    first_hashes = {c.code_hash for c in first}
    assert len(first) == 10
    r = client.post("/settings/totp/backup-codes/regenerate")
    assert r.status_code == 200
    assert b"backup-codes" in r.data
    second = TotpBackupCode.query.filter_by(user_id=user.id).all()
    assert len(second) == 10
    # 旧コードは全て差し替わっている (ハッシュ集合が変わる)
    assert {c.code_hash for c in second}.isdisjoint(first_hashes)


def test_totp_404_when_not_configured(client, db, user, app):
    _login(client, user)
    app.config["LOGIN_SERVER_SECRET"] = ""
    assert client.get("/settings/totp").status_code == 404
    assert client.post("/settings/totp/begin").status_code == 404


def test_password_less_user_cannot_begin_totp(client, db):
    """パスワード未設定ユーザー (password_hash=NULL) は TOTP 登録を開始できない。
    無効化時のパスワード再認証が成立せず永久ロックアウトになるため (#385 PR-T4)。"""
    u = User(username="pko", email="pko@test.com", user_type="personal")
    # set_password しない (password_hash=NULL)
    db.session.add(u)
    db.session.commit()
    _login(client, u)
    r = client.post("/settings/totp/begin")
    assert r.status_code == 302
    assert db.session.get(User, u.id).totp_secret_encrypted is None


def test_confirm_rate_limited(ratelimit_app, ratelimit_client):
    """/totp/confirm はコード総当り抑止のため 5/min で 429 になる (§3.6.4)。"""
    ratelimit_app.config["LOGIN_SERVER_SECRET"] = LOGIN_SECRET
    with ratelimit_app.app_context():
        u = User(username="rl_totp", email="rl_totp@test.com", user_type="personal")
        u.set_password("password123")
        _db.session.add(u)
        _db.session.commit()
        uid = u.id
    with ratelimit_client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    ratelimit_client.post("/settings/totp/begin")
    last = None
    for _ in range(6):
        last = ratelimit_client.post("/settings/totp/confirm", data={"code": "000000"})
    assert last.status_code == 429
