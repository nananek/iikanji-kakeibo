"""パスキー登録の TOTP 必須化ゲート (webauthn.py) のテスト。

新規パスキー登録には TOTP 有効が必要。認証 (authenticate) 経路は TOTP 不問。
"""

from unittest.mock import MagicMock, patch


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)


def test_register_options_blocked_without_totp(client, user):
    """TOTP 未有効では register/options が 403 totp_required。"""
    _login(client, user)
    resp = client.post("/webauthn/register/options")
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["code"] == "totp_required"


def test_register_verify_blocked_without_totp(client, user):
    """TOTP 未有効では register/verify も 403 totp_required (二重ガード)。"""
    _login(client, user)
    resp = client.post("/webauthn/register/verify", json={"id": "x"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "totp_required"


def test_register_options_allowed_with_totp(client, totp_user):
    """TOTP 有効なら register/options はチャレンジ生成に到達する。"""
    _login(client, totp_user)
    with patch("app.views.webauthn.generate_registration_options") as mock_gen, \
         patch("app.views.webauthn.options_to_json") as mock_json:
        opts = MagicMock()
        opts.challenge = b"challenge-bytes"
        mock_gen.return_value = opts
        mock_json.return_value = '{"challenge": "x"}'
        resp = client.post("/webauthn/register/options")
    assert resp.status_code == 200
    mock_gen.assert_called_once()


def test_authenticate_options_does_not_require_totp(client, user):
    """認証 (パスキーログイン) 経路は TOTP 不問。"""
    with patch("app.views.webauthn.generate_authentication_options") as mock_gen, \
         patch("app.views.webauthn.options_to_json") as mock_json:
        opts = MagicMock()
        opts.challenge = b"challenge-bytes"
        mock_gen.return_value = opts
        mock_json.return_value = '{"challenge": "x"}'
        resp = client.post("/webauthn/authenticate/options")
    assert resp.status_code == 200
