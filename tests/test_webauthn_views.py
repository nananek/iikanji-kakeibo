"""WebAuthn (webauthn.py) ビューのテスト

webauthn ライブラリ呼び出しはモック化して、ハンドラのロジック部分のみ
（challenge 取得・エラーパス・open redirect 対策など）をカバー。
"""

from unittest.mock import MagicMock, patch

from app.models.webauthn import WebAuthnCredential


class TestRegisterOptions:
    def test_unauthenticated(self, client):
        resp = client.post("/webauthn/register/options")
        assert resp.status_code in (302, 401)

    def test_authenticated_returns_options(self, logged_in_client, user, accounts):
        with patch("app.views.webauthn.generate_registration_options") as mock_gen, \
             patch("app.views.webauthn.options_to_json") as mock_json:
            mock_options = MagicMock()
            mock_options.challenge = b"challenge-bytes"
            mock_gen.return_value = mock_options
            mock_json.return_value = '{"challenge": "x"}'

            resp = logged_in_client.post("/webauthn/register/options")
            assert resp.status_code == 200
            mock_gen.assert_called_once()


class TestRegisterVerify:
    def test_unauthenticated(self, client):
        resp = client.post("/webauthn/register/verify", json={})
        assert resp.status_code in (302, 401)

    def test_no_challenge_in_session(self, logged_in_client, user, accounts):
        resp = logged_in_client.post("/webauthn/register/verify", json={})
        assert resp.status_code == 400
        body = resp.get_json()
        assert "チャレンジ" in body["error"]

    def test_verify_failure_safe_message(self, logged_in_client, user, accounts):
        with logged_in_client.session_transaction() as sess:
            sess["webauthn_register_challenge"] = b"challenge"
        with patch("app.views.webauthn.verify_registration_response") as mock_verify:
            mock_verify.side_effect = Exception("internal error secret")
            resp = logged_in_client.post(
                "/webauthn/register/verify",
                json={"id": "x", "response": {}},
            )
            assert resp.status_code == 400
            body = resp.get_json()
            # 内部例外メッセージは漏らさない
            assert "internal error secret" not in body["error"]
            assert "検証に失敗" in body["error"]

    def test_verify_success_creates_credential(self, db, logged_in_client, user, accounts):
        with logged_in_client.session_transaction() as sess:
            sess["webauthn_register_challenge"] = b"challenge"
        with patch("app.views.webauthn.verify_registration_response") as mock_verify:
            verification = MagicMock()
            verification.credential_id = b"cred-id-bytes"
            verification.credential_public_key = b"pub-key"
            verification.sign_count = 0
            mock_verify.return_value = verification

            resp = logged_in_client.post(
                "/webauthn/register/verify",
                json={
                    "id": "x",
                    "response": {"transports": ["usb", "nfc"]},
                    "passkey_name": "MyKey",
                },
            )
            assert resp.status_code == 200
            assert resp.get_json()["ok"] is True
            cred = WebAuthnCredential.query.filter_by(user_id=user.id).first()
            assert cred is not None
            assert cred.name == "MyKey"
            assert cred.transports == "usb / nfc"


class TestAuthenticateOptions:
    """認証 options は未ログインでもアクセス可能"""

    def test_returns_options(self, client):
        with patch("app.views.webauthn.generate_authentication_options") as mock_gen, \
             patch("app.views.webauthn.options_to_json") as mock_json:
            mock_options = MagicMock()
            mock_options.challenge = b"auth-challenge"
            mock_gen.return_value = mock_options
            mock_json.return_value = '{"challenge": "y"}'

            resp = client.post("/webauthn/authenticate/options")
            assert resp.status_code == 200


class TestAuthenticateVerify:
    def test_no_challenge(self, client):
        resp = client.post("/webauthn/authenticate/verify", json={})
        assert resp.status_code == 400
        body = resp.get_json()
        assert "チャレンジ" in body["error"]

    def test_invalid_raw_id(self, client):
        with client.session_transaction() as sess:
            sess["webauthn_auth_challenge"] = b"challenge"
        resp = client.post(
            "/webauthn/authenticate/verify",
            json={"rawId": "%%%not-base64%%%"},
        )
        # base64 デコードエラー → 400 か、登録なし → 400
        assert resp.status_code == 400

    def test_unregistered_credential(self, client):
        with client.session_transaction() as sess:
            sess["webauthn_auth_challenge"] = b"challenge"
        # 有効な base64 だがDBに無い
        resp = client.post(
            "/webauthn/authenticate/verify",
            json={"rawId": "AAAA"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "登録されていない" in body["error"]

    def test_verify_success_logs_in(self, db, client, user, accounts):
        # 既存 credential
        cred = WebAuthnCredential(
            user_id=user.id,
            credential_id=b"\x01\x02\x03",
            credential_public_key=b"pub",
            current_sign_count=0,
            name="X",
        )
        db.session.add(cred)
        db.session.commit()

        with client.session_transaction() as sess:
            sess["webauthn_auth_challenge"] = b"challenge"

        with patch("app.views.webauthn.verify_authentication_response") as mock_verify:
            v = MagicMock()
            v.new_sign_count = 1
            mock_verify.return_value = v

            import base64
            raw_id = base64.urlsafe_b64encode(b"\x01\x02\x03").decode().rstrip("=")
            resp = client.post(
                "/webauthn/authenticate/verify",
                json={"rawId": raw_id},
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["ok"] is True
            # ログイン状態
            with client.session_transaction() as sess:
                assert sess.get("_user_id") == str(user.id)

    def test_verify_failure_safe_message(self, db, client, user, accounts):
        cred = WebAuthnCredential(
            user_id=user.id,
            credential_id=b"\xaa\xbb\xcc",
            credential_public_key=b"pub",
            current_sign_count=0,
        )
        db.session.add(cred)
        db.session.commit()

        with client.session_transaction() as sess:
            sess["webauthn_auth_challenge"] = b"challenge"

        with patch("app.views.webauthn.verify_authentication_response") as mock_verify:
            mock_verify.side_effect = Exception("secret internal")
            import base64
            raw_id = base64.urlsafe_b64encode(b"\xaa\xbb\xcc").decode().rstrip("=")
            resp = client.post(
                "/webauthn/authenticate/verify",
                json={"rawId": raw_id},
            )
            assert resp.status_code == 400
            body = resp.get_json()
            assert "secret internal" not in body["error"]

    def test_open_redirect_blocked(self, db, client, user, accounts):
        cred = WebAuthnCredential(
            user_id=user.id,
            credential_id=b"\x10\x20\x30",
            credential_public_key=b"pub",
            current_sign_count=0,
        )
        db.session.add(cred)
        db.session.commit()

        with client.session_transaction() as sess:
            sess["webauthn_auth_challenge"] = b"challenge"

        with patch("app.views.webauthn.verify_authentication_response") as mock_verify:
            v = MagicMock()
            v.new_sign_count = 1
            mock_verify.return_value = v

            import base64
            raw_id = base64.urlsafe_b64encode(b"\x10\x20\x30").decode().rstrip("=")
            resp = client.post(
                "/webauthn/authenticate/verify?next=https://evil.example.com/",
                json={"rawId": raw_id},
            )
            assert resp.status_code == 200
            body = resp.get_json()
            # 外部 URL は破棄される
            assert body["redirect"] == "/"


class TestKeyDerivationOptions:
    """POST /webauthn/key-derivation/options — 鍵派生用 WebAuthn options (E1 PR-F3a)"""

    def test_unauthenticated(self, client):
        resp = client.post("/webauthn/key-derivation/options", json={})
        assert resp.status_code in (302, 401)

    def test_no_passkey_registered(self, logged_in_client, user, accounts):
        resp = logged_in_client.post("/webauthn/key-derivation/options", json={})
        assert resp.status_code == 400
        body = resp.get_json()
        assert "Passkey" in body["error"]

    def test_returns_options_for_owned_passkey(
        self, db, logged_in_client, user, accounts
    ):
        cred = WebAuthnCredential(
            user_id=user.id,
            credential_id=b"\xAA\xBB\xCC",
            credential_public_key=b"pub",
            current_sign_count=0,
        )
        db.session.add(cred)
        db.session.commit()

        with patch("app.views.webauthn.generate_authentication_options") as mock_gen, \
             patch("app.views.webauthn.options_to_json") as mock_json:
            mock_options = MagicMock()
            mock_options.challenge = b"derivation-challenge"
            mock_gen.return_value = mock_options
            mock_json.return_value = '{"challenge": "x"}'

            resp = logged_in_client.post(
                "/webauthn/key-derivation/options", json={}
            )
            assert resp.status_code == 200
            # User Verification は REQUIRED で呼ばれる (PRF は UV 必須)
            call_kwargs = mock_gen.call_args.kwargs
            from webauthn.helpers.structs import UserVerificationRequirement
            assert call_kwargs["user_verification"] == UserVerificationRequirement.REQUIRED

    def test_filter_by_credential_id(self, db, logged_in_client, user, accounts):
        cred_a = WebAuthnCredential(
            user_id=user.id, credential_id=b"\x01",
            credential_public_key=b"pub_a", current_sign_count=0,
        )
        cred_b = WebAuthnCredential(
            user_id=user.id, credential_id=b"\x02",
            credential_public_key=b"pub_b", current_sign_count=0,
        )
        db.session.add_all([cred_a, cred_b])
        db.session.commit()

        with patch("app.views.webauthn.generate_authentication_options") as mock_gen, \
             patch("app.views.webauthn.options_to_json") as mock_json:
            mock_options = MagicMock()
            mock_options.challenge = b"x"
            mock_gen.return_value = mock_options
            mock_json.return_value = "{}"

            resp = logged_in_client.post(
                "/webauthn/key-derivation/options",
                json={"credential_id": cred_b.id},
            )
            assert resp.status_code == 200
            # cred_b のみ allow_credentials に含まれる
            call_kwargs = mock_gen.call_args.kwargs
            allowed = call_kwargs["allow_credentials"]
            assert len(allowed) == 1
            assert allowed[0].id == b"\x02"


class TestKeyDerivationFinalize:
    """POST /webauthn/key-derivation/finalize — 鍵派生 finalize (E1 PR-F3a)"""

    def test_unauthenticated(self, client):
        resp = client.post("/webauthn/key-derivation/finalize", json={})
        assert resp.status_code in (302, 401)

    def test_no_challenge(self, logged_in_client, user, accounts):
        resp = logged_in_client.post(
            "/webauthn/key-derivation/finalize", json={"rawId": "x"}
        )
        assert resp.status_code == 400

    def test_other_user_passkey_rejected(
        self, db, app, client, user, auditor, accounts
    ):
        """他ユーザーの credential_id を投げても 400 で IDOR を防ぐ。"""
        other_cred = WebAuthnCredential(
            user_id=auditor.id,
            credential_id=b"\xFF\xFE",
            credential_public_key=b"pub",
            current_sign_count=0,
        )
        db.session.add(other_cred)
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["webauthn_key_derivation_challenge"] = b"challenge"

        import base64
        raw_id = base64.urlsafe_b64encode(b"\xFF\xFE").decode().rstrip("=")
        resp = client.post(
            "/webauthn/key-derivation/finalize",
            json={"rawId": raw_id, "response": {}},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "ユーザー" in body["error"]

    def test_login_not_called(self, db, logged_in_client, user, accounts):
        """鍵派生 finalize は login_user を呼ばない (session login を発火しない)。"""
        cred = WebAuthnCredential(
            user_id=user.id,
            credential_id=b"\x77\x88",
            credential_public_key=b"pub",
            current_sign_count=0,
        )
        db.session.add(cred)
        db.session.commit()

        with logged_in_client.session_transaction() as sess:
            sess["webauthn_key_derivation_challenge"] = b"challenge"

        import base64
        raw_id = base64.urlsafe_b64encode(b"\x77\x88").decode().rstrip("=")

        with patch("app.views.webauthn.verify_authentication_response") as mock_verify, \
             patch("app.views.webauthn.login_user") as mock_login:
            v = MagicMock()
            v.new_sign_count = 5
            mock_verify.return_value = v
            resp = logged_in_client.post(
                "/webauthn/key-derivation/finalize",
                json={"rawId": raw_id, "response": {}},
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["ok"] is True
            assert body["credential_id"] == cred.id
            # login_user は呼ばれない (session login しない)
            mock_login.assert_not_called()

        # sign_count が更新されている
        db.session.refresh(cred)
        assert cred.current_sign_count == 5

    def test_verify_failure_safe_message(self, db, logged_in_client, user, accounts):
        cred = WebAuthnCredential(
            user_id=user.id,
            credential_id=b"\xAA",
            credential_public_key=b"pub",
            current_sign_count=0,
        )
        db.session.add(cred)
        db.session.commit()

        with logged_in_client.session_transaction() as sess:
            sess["webauthn_key_derivation_challenge"] = b"challenge"

        import base64
        raw_id = base64.urlsafe_b64encode(b"\xAA").decode().rstrip("=")

        with patch("app.views.webauthn.verify_authentication_response") as mock_verify:
            mock_verify.side_effect = Exception("internal stack trace")
            resp = logged_in_client.post(
                "/webauthn/key-derivation/finalize",
                json={"rawId": raw_id, "response": {}},
            )
            assert resp.status_code == 400
            body = resp.get_json()
            assert "internal stack trace" not in body["error"]
