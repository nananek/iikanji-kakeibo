"""顧問招待メール送信のテスト (Phase 6 #71)。

`audit_add` で AuditGrant 作成後に `send_email("audit_invitation", ...)`
が呼ばれることを確認。`ConsoleMailBackend` のダンプを `capsys` で
キャプチャして本文を検証する。
"""

import pytest

from app.models.audit import AuditGrant


class TestAuditInvitationMail:
    def test_invitation_email_is_sent_on_grant(
        self, db, logged_in_client, user, accounts, auditor, capsys
    ):
        from app.views.settings import PERMISSION_LABELS

        resp = logged_in_client.post(
            "/settings/audit/add",
            data={"username": auditor.username, "permission_level": "3"},
        )
        assert resp.status_code in (302, 303)
        assert AuditGrant.query.count() == 1

        out = capsys.readouterr().out
        # 顧問のメールアドレス宛に送信される
        assert f"To:   {auditor.email}" in out
        # 件名に owner のユーザー名 + MAIL_FROM_NAME (= "いいかんじ™家計簿")
        # が含まれる
        assert f"{user.username}" in out
        assert "顧問アクセスの招待" in out
        # 件名のブランド名プレフィックスが config 由来
        assert "[いいかんじ™家計簿]" in out
        # 本文の権限レベル (PERMISSION_LABELS の値を直接参照)
        assert PERMISSION_LABELS[3] in out
        # フッターも MAIL_FROM_NAME 経由
        assert "いいかんじ™家計簿" in out

    def test_invitation_omitted_when_auditor_has_no_email(
        self, db, logged_in_client, user, accounts, capsys
    ):
        """auditor の email が空のときはメール送信を試みない"""
        from app.models.user import User
        no_email_auditor = User(
            username="no_email_auditor",
            email="",
            user_type="auditor",
        )
        no_email_auditor.set_password("password123")
        db.session.add(no_email_auditor)
        db.session.commit()

        resp = logged_in_client.post(
            "/settings/audit/add",
            data={"username": no_email_auditor.username, "permission_level": "3"},
        )
        assert resp.status_code in (302, 303)
        # AuditGrant は作成される
        assert AuditGrant.query.count() == 1
        out = capsys.readouterr().out
        # メール送信ダンプは含まれない
        assert "audit_invitation" not in out
        assert "[Mail]" not in out

    def test_invitation_failure_does_not_break_flow(
        self, db, logged_in_client, user, accounts, auditor, monkeypatch
    ):
        """メール送信が失敗しても AuditGrant 作成自体は成功扱い"""
        from app.services import mail as mail_mod
        from app.services.mail import ConsoleMailBackend

        class FailingBackend(ConsoleMailBackend):
            def send(self, to, from_addr, rendered):
                raise RuntimeError("simulated SMTP failure")

        monkeypatch.setattr(mail_mod, "get_mail_backend", lambda: FailingBackend())

        resp = logged_in_client.post(
            "/settings/audit/add",
            data={"username": auditor.username, "permission_level": "3"},
        )
        assert resp.status_code in (302, 303)
        assert AuditGrant.query.count() == 1


class TestFooterUsesMailFromName:
    """フッターが `MAIL_FROM_NAME` から動的に組み立てられること (申し送り 1)"""

    def test_footer_reflects_config(self, app, monkeypatch):
        from app.services.mail import render_email

        monkeypatch.setitem(app.config, "MAIL_FROM_NAME", "Custom Brand")
        with app.test_request_context():
            rendered = render_email(
                "_skeleton",
                {"subject": "件名", "body": "本文"},
            )
        assert "Custom Brand" in rendered.text_body
        # default のいいかんじブランドは出ない
        assert "いいかんじ" not in rendered.text_body
