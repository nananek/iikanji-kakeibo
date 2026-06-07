"""TOTP 登録/確認/無効化フロー (settings views) のテスト。"""

import pyotp

from app.models.user import User
from app.services import totp as totp_svc


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)


def test_begin_stores_secret_but_not_enabled(client, user, db):
    _login(client, user)
    resp = client.post("/settings/totp/begin")
    assert resp.status_code == 200
    assert b"<svg" in resp.data  # QR が表示される
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_secret_encrypted is not None
    assert refreshed.totp_enabled is False


def test_confirm_with_valid_code_enables(client, user, db):
    _login(client, user)
    client.post("/settings/totp/begin")
    refreshed = db.session.get(User, user.id)
    secret = totp_svc.decrypt_secret(refreshed.totp_secret_encrypted)
    code = pyotp.TOTP(secret).now()

    resp = client.post("/settings/totp/confirm", data={"code": code},
                       follow_redirects=True)
    assert resp.status_code == 200
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_enabled is True
    assert refreshed.totp_confirmed_at is not None
    assert refreshed.totp_last_used_step is not None


def test_confirm_with_wrong_code_does_not_enable(client, user, db):
    _login(client, user)
    client.post("/settings/totp/begin")
    resp = client.post("/settings/totp/confirm", data={"code": "000000"})
    assert resp.status_code == 200
    assert b"<svg" in resp.data  # 失敗時は再度 QR を表示
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_enabled is False


def test_confirm_without_begin_redirects(client, user, db):
    _login(client, user)
    resp = client.post("/settings/totp/confirm", data={"code": "123456"})
    assert resp.status_code == 302
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_enabled is False


def test_cancel_clears_unconfirmed_secret_without_password(client, user, db):
    _login(client, user)
    client.post("/settings/totp/begin")
    resp = client.post("/settings/totp/cancel", follow_redirects=True)
    assert resp.status_code == 200
    refreshed = db.session.get(User, user.id)
    assert refreshed.totp_secret_encrypted is None
    assert refreshed.totp_enabled is False


def test_disable_requires_correct_password(client, totp_user, db):
    _login(client, totp_user)
    # 誤パスワードでは無効化されない
    resp = client.post("/settings/totp/disable", data={"password": "wrong"},
                       follow_redirects=True)
    assert resp.status_code == 200
    refreshed = db.session.get(User, totp_user.id)
    assert refreshed.totp_enabled is True

    # 正しいパスワードで無効化
    resp = client.post("/settings/totp/disable",
                       data={"password": "password123"}, follow_redirects=True)
    assert resp.status_code == 200
    refreshed = db.session.get(User, totp_user.id)
    assert refreshed.totp_enabled is False
    assert refreshed.totp_secret_encrypted is None
    assert refreshed.totp_last_used_step is None


def test_begin_when_already_enabled_is_noop(client, totp_user, db):
    _login(client, totp_user)
    resp = client.post("/settings/totp/begin", follow_redirects=True)
    assert resp.status_code == 200
    refreshed = db.session.get(User, totp_user.id)
    # 既存の有効状態は変わらない
    assert refreshed.totp_enabled is True


def test_totp_status_page_renders(client, user):
    _login(client, user)
    resp = client.get("/settings/totp")
    assert resp.status_code == 200
    assert "二段階認証".encode() in resp.data


def test_status_shows_enrolling_message_after_begin(client, user, db):
    """begin 後 (secret あり・未有効) は登録途中メッセージを表示する。"""
    _login(client, user)
    client.post("/settings/totp/begin")
    resp = client.get("/settings/totp")
    assert resp.status_code == 200
    assert "登録途中".encode() in resp.data
