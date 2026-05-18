"""メール配信基盤 (`app/services/mail.py`) のテスト (Phase 6 #71)。

Phase 6 最初の PR の骨格部分:
- `ConsoleMailBackend` の挙動
- `get_mail_backend()` のファクトリ切替
- `render_email` の Jinja レンダリング (text のみ / text + html 両方)
- `send_email` の API 結合
- From アドレスの整形 (`Name <addr>` / `addr` のみ / 空)
"""

import io

import pytest

from app.services.mail import (
    ConsoleMailBackend,
    MailBackend,
    RenderedEmail,
    _format_from_address,
    get_mail_backend,
    render_email,
    send_email,
)


# --- ConsoleMailBackend ----------------------------------------------------


class TestConsoleMailBackend:
    def test_is_subclass(self):
        assert issubclass(ConsoleMailBackend, MailBackend)

    def test_dumps_text_only(self, capsys):
        backend = ConsoleMailBackend()
        rendered = RenderedEmail(
            subject="テスト件名", text_body="プレーン本文"
        )
        backend.send("user@example.com", "noreply@example.com", rendered)
        out = capsys.readouterr().out
        assert "To:   user@example.com" in out
        assert "From: noreply@example.com" in out
        assert "テスト件名" in out
        assert "プレーン本文" in out
        # HTML 行は含まれない
        assert "(HTML)" not in out

    def test_dumps_text_and_html(self, capsys):
        backend = ConsoleMailBackend()
        rendered = RenderedEmail(
            subject="件名", text_body="text 本文", html_body="<p>html</p>"
        )
        backend.send("u@example.com", "noreply@example.com", rendered)
        out = capsys.readouterr().out
        assert "text 本文" in out
        assert "(HTML)" in out
        assert "<p>html</p>" in out

    def test_dumps_extra_headers(self, capsys):
        backend = ConsoleMailBackend()
        rendered = RenderedEmail(
            subject="件名", text_body="本文",
            headers={"X-Idempotency-Key": "abc123"},
        )
        backend.send("u@example.com", "noreply@example.com", rendered)
        out = capsys.readouterr().out
        assert "X-Idempotency-Key: abc123" in out


# --- ファクトリ -------------------------------------------------------------


class TestGetMailBackend:
    def test_default_is_console(self, app):
        with app.test_request_context():
            backend = get_mail_backend()
            assert isinstance(backend, ConsoleMailBackend)

    def test_explicit_console(self, app, monkeypatch):
        monkeypatch.setitem(app.config, "MAIL_BACKEND", "console")
        with app.test_request_context():
            backend = get_mail_backend()
            assert isinstance(backend, ConsoleMailBackend)

    @pytest.mark.parametrize("backend_name", ["smtp", "ses", "resend"])
    def test_unimplemented_backends_raise(self, app, monkeypatch, backend_name):
        monkeypatch.setitem(app.config, "MAIL_BACKEND", backend_name)
        with app.test_request_context():
            with pytest.raises(NotImplementedError):
                get_mail_backend()


# --- render_email -----------------------------------------------------------


class TestRenderEmail:
    """`_skeleton` テンプレートを使ったレンダリング検証"""

    def test_renders_subject_stripped(self, app):
        with app.test_request_context():
            rendered = render_email(
                "_skeleton",
                {"subject": "件名テスト", "body": "本文"},
            )
            # subject.txt の末尾改行は strip されること
            assert rendered.subject == "件名テスト"

    def test_renders_text_body(self, app):
        with app.test_request_context():
            rendered = render_email(
                "_skeleton",
                {"subject": "件名", "greeting": "こんにちは Alice", "body": "本文 X"},
            )
            assert "こんにちは Alice" in rendered.text_body
            assert "本文 X" in rendered.text_body

    def test_html_body_optional(self, app):
        """`body.html` が存在しないテンプレートは text のみで返る"""
        with app.test_request_context():
            rendered = render_email(
                "_skeleton",
                {"subject": "件名", "body": "本文"},
            )
            assert rendered.html_body is None


# --- send_email -------------------------------------------------------------


class TestSendEmail:
    def test_backend_failure_is_swallowed(self, app, monkeypatch):
        """`backend.send` が例外を出しても `send_email` は呼び出し側に
        伝播させず、ログだけ残す (Phase 6 設計方針: 失敗は本体フローに
        影響させない)。
        """
        class FailingBackend(ConsoleMailBackend):
            def send(self, to, from_addr, rendered):
                raise RuntimeError("simulated SMTP failure")

        from app.services import mail as mail_mod
        monkeypatch.setattr(
            mail_mod, "get_mail_backend", lambda: FailingBackend()
        )

        with app.test_request_context():
            # 例外を投げずに完了する
            send_email("u@example.com", "_skeleton", {"body": "x"})

    def test_send_with_console_backend(self, app, capsys):
        with app.test_request_context():
            send_email(
                "user@example.com",
                "_skeleton",
                {"subject": "テスト", "body": "送信検証"},
            )
        out = capsys.readouterr().out
        assert "To:   user@example.com" in out
        assert "送信検証" in out

    def test_context_default_empty(self, app, capsys):
        """context 未指定でもエラーにならず、テンプレートのデフォルト値が使われる"""
        with app.test_request_context():
            send_email("u@example.com", "_skeleton", {"body": "x"})
        out = capsys.readouterr().out
        assert "u@example.com" in out

    def test_from_address_uses_config(self, app, capsys, monkeypatch):
        monkeypatch.setitem(app.config, "MAIL_FROM", "alerts@example.com")
        monkeypatch.setitem(app.config, "MAIL_FROM_NAME", "テスト送信")
        with app.test_request_context():
            send_email("u@example.com", "_skeleton", {"body": "x"})
        out = capsys.readouterr().out
        assert "From: テスト送信 <alerts@example.com>" in out


# --- _format_from_address ---------------------------------------------------


class TestFormatFromAddress:
    @pytest.mark.parametrize(
        "addr, name, expected",
        [
            ("noreply@example.com", "Iikanji", "Iikanji <noreply@example.com>"),
            ("noreply@example.com", "", "noreply@example.com"),
            ("", "Iikanji", "Iikanji"),
            ("", "", ""),
        ],
    )
    def test_formats(self, addr, name, expected):
        assert _format_from_address(addr, name) == expected


# --- インターフェース整合 ---------------------------------------------------


class TestMailBackendAbstract:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            MailBackend()
