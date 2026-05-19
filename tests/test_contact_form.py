"""お問い合わせフォーム (Phase 4 公開運用整備)."""

from unittest.mock import patch

import pytest


@pytest.fixture
def reset_limiter(app):
    try:
        from app.extensions import limiter
        limiter.reset()
    except Exception:
        pass
    yield


class TestContactGet:
    def test_get_renders_form(self, client, reset_limiter):
        resp = client.get("/legal/contact")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "お問い合わせ" in body
        assert "name=\"name\"" in body
        assert "name=\"email\"" in body
        assert "name=\"message\"" in body

    def test_accessible_without_login(self, client, reset_limiter):
        """未認証でもアクセスできる (退会済ユーザー等が利用)."""
        resp = client.get("/legal/contact")
        assert resp.status_code == 200

    def test_accessible_to_pending_recovery_user(
        self, app, logged_in_client, db, user, reset_limiter,
    ):
        """リカバリログイン後の強制復旧フロー中でもアクセス可能."""
        with logged_in_client.session_transaction() as sess:
            sess["pending_recovery_action"] = True
            sess["pending_recovery_user_id"] = user.id
        resp = logged_in_client.get("/legal/contact")
        assert resp.status_code == 200


class TestContactPost:
    def _form_data(self, **overrides):
        data = {
            "name": "山田太郎",
            "email": "yamada@example.com",
            "subject_line": "テスト件名",
            "message": "問い合わせ本文を 10 文字以上で書きます。",
        }
        data.update(overrides)
        return data

    def test_post_sends_two_emails(
        self, client, app, monkeypatch, reset_limiter,
    ):
        """送信成功時、運営者宛 + 送信者宛の 2 通が send_email() に渡る."""
        monkeypatch.setitem(app.config, "MAIL_CONTACT_TO", "admin@example.com")
        sent = []

        def fake_send(to, template_name, context=None, **kwargs):
            sent.append((to, template_name, context or {}))

        with patch("app.views.legal.send_email", side_effect=fake_send):
            resp = client.post("/legal/contact", data=self._form_data())

        assert resp.status_code == 302
        assert len(sent) == 2
        admin_call = next(s for s in sent if s[1] == "contact_received_admin")
        user_call = next(s for s in sent if s[1] == "contact_received")
        assert admin_call[0] == "admin@example.com"
        assert user_call[0] == "yamada@example.com"
        assert admin_call[2]["name"] == "山田太郎"
        assert admin_call[2]["message"].startswith("問い合わせ本文")
        assert user_call[2]["email"] == "yamada@example.com"

    def test_post_without_contact_to_skips_admin(
        self, client, app, monkeypatch, reset_limiter,
    ):
        """MAIL_CONTACT_TO 未設定なら運営者宛は送らず、自動返信のみ."""
        monkeypatch.setitem(app.config, "MAIL_CONTACT_TO", "")
        sent = []
        with patch(
            "app.views.legal.send_email",
            side_effect=lambda to, t, ctx=None, **kw: sent.append((to, t)),
        ):
            resp = client.post("/legal/contact", data=self._form_data())

        assert resp.status_code == 302
        assert len(sent) == 1
        assert sent[0] == ("yamada@example.com", "contact_received")

    def test_post_validation_short_message(self, client, reset_limiter):
        resp = client.post("/legal/contact", data={
            "name": "山田", "email": "y@example.com",
            "subject_line": "", "message": "短い",
        })
        # 短すぎる message は再描画 (200)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "10" in body or "Field must" in body

    def test_post_validation_missing_email(self, client, reset_limiter):
        resp = client.post("/legal/contact", data={
            "name": "山田", "email": "",
            "subject_line": "", "message": "テスト本文 10 文字以上書く",
        })
        assert resp.status_code == 200

    def test_send_email_failure_does_not_propagate(
        self, client, app, monkeypatch, reset_limiter,
    ):
        """send_email() の例外で 500 にしない (UX 維持)."""
        monkeypatch.setitem(app.config, "MAIL_CONTACT_TO", "admin@example.com")

        def fake_send_raises(to, t, ctx=None, **kw):
            raise RuntimeError("SMTP down")

        with patch(
            "app.views.legal.send_email", side_effect=fake_send_raises,
        ):
            resp = client.post("/legal/contact", data=self._form_data())

        # 例外を握って 302 (リダイレクト)、ユーザーには成功 flash
        assert resp.status_code == 302

    # rate limit (5/hour) は TestConfig.RATELIMIT_ENABLED=False のため
    # 本テストファイルでは検証しない。専用 fixture (RateLimitTestConfig)
    # を導入する場合は test_security_ratelimit.py 側に追加すること。
