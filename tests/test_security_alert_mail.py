"""パスキー追加検知時のセキュリティアラートメール (Phase 6 #71)。

`webauthn.register_verify` でパスキー登録成功時に `send_email(
"security_alert", ...)` が呼ばれることを確認。
"""

from unittest.mock import MagicMock, patch

from app.models.webauthn import WebAuthnCredential


def _post_register_verify(client, *, transports=("usb",), passkey_name="TestKey"):
    """register_verify を成功させる共通ヘルパー"""
    with client.session_transaction() as sess:
        sess["webauthn_register_challenge"] = b"challenge"
    with patch("app.views.webauthn.verify_registration_response") as mock_verify:
        verification = MagicMock()
        verification.credential_id = b"cred-id-bytes"
        verification.credential_public_key = b"pub-key"
        verification.sign_count = 0
        mock_verify.return_value = verification
        return client.post(
            "/webauthn/register/verify",
            json={
                "id": "x",
                "response": {"transports": list(transports)},
                "passkey_name": passkey_name,
            },
        )


class TestPasskeyAddedAlert:
    def test_alert_sent_on_success(self, db, logged_in_client, user, accounts, capsys):
        resp = _post_register_verify(logged_in_client)
        assert resp.status_code == 200

        out = capsys.readouterr().out
        # 監査招待と同じ ConsoleMailBackend ダンプの形式
        assert f"To:   {user.email}" in out
        assert "セキュリティ通知" in out
        assert "新しいパスキーが追加されました" in out
        # context 注入確認
        assert user.username in out
        assert "TestKey" in out
        assert "usb" in out

    def test_alert_skipped_when_email_empty(
        self, db, client, accounts, capsys
    ):
        from app.models.user import User
        no_email_user = User(
            username="no_email_pk",
            email="",
            user_type="personal",
        )
        no_email_user.set_password("password123")
        db.session.add(no_email_user)
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(no_email_user.id)

        resp = _post_register_verify(client)
        assert resp.status_code == 200
        # WebAuthn 登録自体は成功
        assert WebAuthnCredential.query.filter_by(
            user_id=no_email_user.id
        ).count() == 1
        # メールダンプは出ない
        out = capsys.readouterr().out
        assert "security_alert" not in out
        assert "[Mail]" not in out

    def test_alert_failure_does_not_break_passkey_registration(
        self, db, logged_in_client, user, accounts, monkeypatch
    ):
        """メール送信失敗でもパスキー登録は成功扱い"""
        from app.services import mail as mail_mod
        from app.services.mail import ConsoleMailBackend

        class FailingBackend(ConsoleMailBackend):
            def send(self, to, from_addr, rendered):
                raise RuntimeError("simulated SMTP failure")

        monkeypatch.setattr(mail_mod, "get_mail_backend", lambda: FailingBackend())

        resp = _post_register_verify(logged_in_client)
        assert resp.status_code == 200
        assert WebAuthnCredential.query.filter_by(user_id=user.id).count() == 1


class TestAlertContent:
    """メール本文に重要なコンテキスト情報が含まれる"""

    def test_includes_ip_and_user_agent(
        self, db, logged_in_client, user, accounts, capsys
    ):
        # Flask test client ではデフォルトの remote_addr / User-Agent が
        # 設定される。それらが本文に出ることを確認。
        resp = _post_register_verify(logged_in_client)
        assert resp.status_code == 200
        out = capsys.readouterr().out
        # IP アドレス / User-Agent のラベルが含まれる
        assert "IP アドレス" in out
        assert "User-Agent" in out
        # 日時 ラベル
        assert "日時" in out
