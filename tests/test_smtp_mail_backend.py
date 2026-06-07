"""SmtpMailBackend と Phase 6 続編テンプレートのテスト (Phase 6 #71)."""

from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest

from app.services.mail import (
    RenderedEmail,
    SmtpMailBackend,
    get_mail_backend,
    render_email,
)


class TestGetMailBackendSmtp:
    def test_smtp_selected(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "MAIL_BACKEND", "smtp")
            backend = get_mail_backend()
            assert isinstance(backend, SmtpMailBackend)

    def test_unknown_still_raises(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "MAIL_BACKEND", "ses")
            with pytest.raises(NotImplementedError):
                get_mail_backend()


class TestSmtpMailBackendSend:
    def _rendered(self):
        return RenderedEmail(
            subject="テスト件名 ™",
            text_body="本文プレーン",
            html_body="<p>本文 HTML</p>",
        )

    def test_raises_without_host(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "MAIL_SMTP_HOST", "")
            backend = SmtpMailBackend()
            with pytest.raises(RuntimeError, match="MAIL_SMTP_HOST"):
                backend.send("to@example.com", "from@example.com", self._rendered())

    def test_starttls_flow(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "MAIL_SMTP_HOST", "smtp.example.com")
            monkeypatch.setitem(app.config, "MAIL_SMTP_PORT", 587)
            monkeypatch.setitem(app.config, "MAIL_SMTP_USERNAME", "user")
            monkeypatch.setitem(app.config, "MAIL_SMTP_PASSWORD", "pass")
            monkeypatch.setitem(app.config, "MAIL_SMTP_USE_TLS", "starttls")

            mock_smtp = MagicMock()
            mock_smtp_class = MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=mock_smtp),
                    __exit__=MagicMock(return_value=False),
                )
            )
            with patch("app.services.mail.smtplib.SMTP", mock_smtp_class):
                backend = SmtpMailBackend()
                backend.send(
                    "user@example.com", "noreply@example.com",
                    self._rendered(),
                )

            mock_smtp_class.assert_called_once_with(
                "smtp.example.com", 587, timeout=30,
            )
            assert mock_smtp.starttls.called
            mock_smtp.login.assert_called_once_with("user", "pass")
            assert mock_smtp.send_message.called
            sent_msg = mock_smtp.send_message.call_args[0][0]
            assert isinstance(sent_msg, EmailMessage)
            assert sent_msg["To"] == "user@example.com"
            assert sent_msg["From"] == "noreply@example.com"
            assert sent_msg.get_content_type() == "multipart/alternative"

    def test_ssl_flow(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "MAIL_SMTP_HOST", "smtp.example.com")
            monkeypatch.setitem(app.config, "MAIL_SMTP_PORT", 465)
            monkeypatch.setitem(app.config, "MAIL_SMTP_USE_TLS", "ssl")
            monkeypatch.setitem(app.config, "MAIL_SMTP_USERNAME", "")
            monkeypatch.setitem(app.config, "MAIL_SMTP_PASSWORD", "")

            mock_smtp = MagicMock()
            mock_smtp_class = MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=mock_smtp),
                    __exit__=MagicMock(return_value=False),
                )
            )
            with patch("app.services.mail.smtplib.SMTP_SSL", mock_smtp_class):
                backend = SmtpMailBackend()
                backend.send(
                    "user@example.com", "noreply@example.com",
                    self._rendered(),
                )

            mock_smtp_class.assert_called_once()
            args, _kwargs = mock_smtp_class.call_args
            assert args[0] == "smtp.example.com"
            assert args[1] == 465
            assert not mock_smtp.starttls.called
            # 認証情報なし → login も呼ばれない
            assert not mock_smtp.login.called
            assert mock_smtp.send_message.called

    def test_none_tls_skips_starttls(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "MAIL_SMTP_HOST", "localhost")
            monkeypatch.setitem(app.config, "MAIL_SMTP_PORT", 25)
            monkeypatch.setitem(app.config, "MAIL_SMTP_USE_TLS", "none")
            monkeypatch.setitem(app.config, "MAIL_SMTP_USERNAME", "")
            monkeypatch.setitem(app.config, "MAIL_SMTP_PASSWORD", "")

            mock_smtp = MagicMock()
            mock_smtp_class = MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=mock_smtp),
                    __exit__=MagicMock(return_value=False),
                )
            )
            with patch("app.services.mail.smtplib.SMTP", mock_smtp_class):
                backend = SmtpMailBackend()
                backend.send(
                    "user@example.com", "noreply@example.com",
                    self._rendered(),
                )

            assert not mock_smtp.starttls.called
            assert not mock_smtp.login.called
            assert mock_smtp.send_message.called

    def test_invalid_use_tls_raises(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "MAIL_SMTP_HOST", "smtp.example.com")
            monkeypatch.setitem(app.config, "MAIL_SMTP_USE_TLS", "garbage")

            mock_smtp = MagicMock()
            mock_smtp_class = MagicMock(
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=mock_smtp),
                    __exit__=MagicMock(return_value=False),
                )
            )
            with patch("app.services.mail.smtplib.SMTP", mock_smtp_class):
                backend = SmtpMailBackend()
                with pytest.raises(RuntimeError, match="MAIL_SMTP_USE_TLS"):
                    backend.send(
                        "to@example.com", "from@example.com", self._rendered(),
                    )


# テンプレ rendering テスト: context_processor (`inject_audit_context`
# 等) が `current_user` を要求するため `test_request_context` 内で実行。


class TestContactReceivedTemplate:
    def test_renders_basic(self, app):
        with app.test_request_context():
            rendered = render_email("contact_received", {
                "name": "山田太郎",
                "email": "yamada@example.com",
                "subject_line": "請求書の出力について",
                "message": "請求書 PDF が文字化けします。",
            })
        assert "お問い合わせを受け付けました" in rendered.subject
        assert "山田太郎" in rendered.text_body
        assert "yamada@example.com" in rendered.text_body
        assert "請求書の出力について" in rendered.text_body
        assert "文字化け" in rendered.text_body

    def test_renders_without_subject_line(self, app):
        with app.test_request_context():
            rendered = render_email("contact_received", {
                "name": "山田",
                "email": "y@example.com",
                "subject_line": "",
                "message": "テスト本文",
            })
        assert "テスト本文" in rendered.text_body


class TestAccountDeletedTemplate:
    def test_renders(self, app):
        with app.test_request_context():
            rendered = render_email("account_deleted", {
                "username": "testuser",
                "deleted_at": "2026-05-19 10:00 JST",
            })
        assert "アカウント削除のお知らせ" in rendered.subject
        assert "testuser" in rendered.text_body
        assert "2026-05-19 10:00 JST" in rendered.text_body


class TestQuotaWarningTemplate:
    def test_renders_warning_level(self, app):
        with app.test_request_context():
            rendered = render_email("quota_warning", {
                "username": "testuser",
                "percentage": 85,
                "used_mb": 425.0,
                "quota_mb": 500.0,
                "level": "warning",
                "settings_url": "https://example.com/settings/",
            })
        assert "85%" in rendered.subject
        assert "85%" in rendered.text_body
        assert "425.0 MB" in rendered.text_body
        assert "500.0 MB" in rendered.text_body
        assert "警告" in rendered.text_body
        assert "https://example.com/settings/" in rendered.text_body

    def test_renders_critical_level(self, app):
        with app.test_request_context():
            rendered = render_email("quota_warning", {
                "username": "testuser",
                "percentage": 97,
                "used_mb": 485.0,
                "quota_mb": 500.0,
                "level": "critical",
                "settings_url": "https://example.com/settings/",
            })
        assert "97%" in rendered.subject
        assert "残量わずか" in rendered.text_body
