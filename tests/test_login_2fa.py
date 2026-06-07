"""ログイン時の TOTP 2FA ゲート (auth.py) のテスト。"""

import time


from tests.conftest import totp_code_for


def _post_login(client, username="testuser", password="password123", next_=None):
    url = "/login"
    if next_ is not None:
        url += f"?next={next_}"
    return client.post(url, data={"username": username, "password": password})


def test_non_totp_user_logs_in_directly(client, user):
    resp = _post_login(client)
    assert resp.status_code == 302
    assert "/login/totp" not in resp.headers["Location"]


def test_totp_user_redirected_to_gate_not_authenticated(client, totp_user, app):
    resp = _post_login(client)
    assert resp.status_code == 302
    assert "/login/totp" in resp.headers["Location"]
    # まだ認証は完了していない (pending セッションのみ)
    with client.session_transaction() as sess:
        assert sess.get("pending_2fa_user_id") == totp_user.id
        assert "_user_id" not in sess


def test_valid_code_completes_login_and_clears_pending(client, totp_user):
    _post_login(client)
    code = totp_code_for(totp_user._test_totp_secret)
    resp = client.post("/login/totp", data={"code": code})
    assert resp.status_code == 302
    assert "/login/totp" not in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert sess.get("_user_id") == str(totp_user.id)
        assert "pending_2fa_user_id" not in sess


def test_wrong_code_stays_on_gate(client, totp_user):
    _post_login(client)
    resp = client.post("/login/totp", data={"code": "000000"})
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert "_user_id" not in sess
        # pending は維持される
        assert sess.get("pending_2fa_user_id") == totp_user.id


def test_replayed_code_rejected_on_second_login(client, totp_user, db):
    from app.models.user import User

    # 1 回目: コードでログイン成功
    _post_login(client)
    code = totp_code_for(totp_user._test_totp_secret)
    resp1 = client.post("/login/totp", data={"code": code})
    assert resp1.status_code == 302
    # ログアウト
    client.get("/logout")

    # 2 回目: 同じコード (同一 step) はリプレイとして拒否される
    _post_login(client)
    resp2 = client.post("/login/totp", data={"code": code})
    assert resp2.status_code == 200
    with client.session_transaction() as sess:
        assert "_user_id" not in sess
    refreshed = db.session.get(User, totp_user.id)
    assert refreshed.totp_last_used_step is not None


def test_next_param_preserved_through_gate(client, totp_user):
    _post_login(client, next_="/settings/")
    with client.session_transaction() as sess:
        assert sess.get("pending_2fa_next") == "/settings/"
    code = totp_code_for(totp_user._test_totp_secret)
    resp = client.post("/login/totp", data={"code": code})
    assert resp.headers["Location"].endswith("/settings/")


def test_external_next_ignored(client, totp_user):
    _post_login(client, next_="https://evil.example.com/")
    with client.session_transaction() as sess:
        # 外部 URL は保存されず内部既定にフォールバック
        assert sess.get("pending_2fa_next") != "https://evil.example.com/"
    code = totp_code_for(totp_user._test_totp_secret)
    resp = client.post("/login/totp", data={"code": code})
    loc = resp.headers["Location"]
    assert "evil.example.com" not in loc


def test_auditor_totp_gate(client, db):
    from app.models.user import User
    from app.services import totp as totp_svc

    aud = User(username="aud2fa", email="aud2fa@example.com", user_type="auditor")
    aud.set_password("password123")
    secret = totp_svc.generate_secret()
    aud.totp_secret_encrypted = totp_svc.encrypt_secret(secret)
    aud.totp_enabled = True
    db.session.add(aud)
    db.session.commit()

    resp = client.post(
        "/login/auditor", data={"username": "aud2fa", "password": "password123"}
    )
    assert resp.status_code == 302
    assert "/login/totp" in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert sess.get("pending_2fa_kind") == "auditor"

    code = totp_code_for(secret)
    resp2 = client.post("/login/totp", data={"code": code})
    assert resp2.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("_user_id") == str(aud.id)


def test_expired_pending_redirects_to_login(client, totp_user):
    _post_login(client)
    # pending タイムスタンプを過去にする
    with client.session_transaction() as sess:
        sess["pending_2fa_ts"] = time.time() - 1000
    resp = client.get("/login/totp")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")
    with client.session_transaction() as sess:
        assert "pending_2fa_user_id" not in sess


def test_gate_without_pending_redirects_to_login(client, user):
    resp = client.get("/login/totp")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")
