"""パスキー専用モード + リカバリコード撤去 (PR5) の確認テスト。

撤去されたルートが 404 を返すこと、User からカラム/メソッドが消えたことを検証。
"""

from app.models.user import User


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)


def test_recovery_login_route_removed(client):
    resp = client.get("/recovery")
    assert resp.status_code == 404


def test_recovery_generate_route_removed(client, user):
    _login(client, user)
    resp = client.post("/settings/passkeys/recovery/generate",
                       data={"password": "password123"})
    assert resp.status_code == 404


def test_passkey_only_enable_route_removed(client, user):
    _login(client, user)
    resp = client.post("/settings/passkeys/passkey-only/enable")
    assert resp.status_code == 404


def test_passkey_only_disable_route_removed(client, user):
    _login(client, user)
    resp = client.post("/settings/passkeys/passkey-only/disable",
                       data={"password": "password123"})
    assert resp.status_code == 404


def test_user_model_has_no_recovery_or_passkey_only_attrs():
    # カラム・メソッドが除去されている
    for attr in (
        "passkey_only_login",
        "recovery_code_hash",
        "recovery_code_prefix",
        "recovery_code_created_at",
        "recovery_code_used_at",
        "set_recovery_code",
        "verify_recovery_code",
        "consume_recovery_code",
        "has_active_recovery_code",
    ):
        assert not hasattr(User, attr), f"User should no longer have {attr}"


def test_password_login_works_for_former_passkey_only_user(client, user):
    """旧パスキー専用ユーザーもパスワードでログインできる (TOTP 未設定時は直接)."""
    resp = client.post(
        "/login", data={"username": "testuser", "password": "password123"}
    )
    assert resp.status_code == 302
    assert "/login/totp" not in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert sess.get("_user_id") == str(user.id)
