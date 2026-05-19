"""Phase 6 #71: 規約改訂通知メールの CLI コマンド notify-terms-update."""

import pytest

from app.models.user import User


CURRENT_VERSION = "2026-05-19"


def _make_user(db, username, email, accepted_version, user_type="personal"):
    u = User(
        username=username,
        email=email,
        user_type=user_type,
        accepted_terms_version=accepted_version,
    )
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    return u


class TestNotifyTermsUpdateTargeting:
    """対象ユーザーの抽出ロジック"""

    def test_dry_run_lists_targets_only(self, db, app, monkeypatch):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        _make_user(db, "target_null", "null@example.com", None)
        _make_user(db, "target_old", "old@example.com", "2026-01-01")
        _make_user(db, "uptodate", "ok@example.com", CURRENT_VERSION)
        _make_user(db, "no_email", "", None)

        runner = app.test_cli_runner()
        result = runner.invoke(args=["notify-terms-update", "--dry-run"])
        out = result.output
        assert "対象ユーザー: 2 件" in out
        assert "target_null" in out
        assert "target_old" in out
        assert "uptodate" not in out
        assert "no_email" not in out

    def test_send_excludes_uptodate_and_no_email(
        self, db, app, monkeypatch
    ):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        _make_user(db, "target1", "t1@example.com", None)
        _make_user(db, "uptodate", "u@example.com", CURRENT_VERSION)
        _make_user(db, "no_email", "", "2026-01-01")

        runner = app.test_cli_runner()
        result = runner.invoke(args=["notify-terms-update"])
        out = result.output
        # 対象ユーザーだけに送信 (ConsoleMailBackend ダンプは stdout に出る)
        assert "To:   t1@example.com" in out
        assert "u@example.com" not in out

    def test_no_op_when_current_version_empty(self, db, app, monkeypatch):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        _make_user(db, "target", "t@example.com", None)

        runner = app.test_cli_runner()
        result = runner.invoke(args=["notify-terms-update"])
        assert "未設定のため" in result.output

    def test_limit_caps_targets(self, db, app, monkeypatch):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        for i in range(5):
            _make_user(db, f"u{i}", f"u{i}@example.com", None)

        runner = app.test_cli_runner()
        result = runner.invoke(
            args=["notify-terms-update", "--dry-run", "--limit", "2"]
        )
        assert "対象ユーザー: 2 件" in result.output


class TestNotifyTermsUpdateContent:
    """送信メールの内容に必要な情報が含まれる"""

    def test_email_contains_required_fields(self, db, app, monkeypatch):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        _make_user(db, "alice", "alice@example.com", None)

        runner = app.test_cli_runner()
        result = runner.invoke(args=["notify-terms-update"])
        out = result.output
        assert "alice" in out
        assert "Subject" in out
        assert CURRENT_VERSION in out
        assert "/legal/terms" in out
        assert "/legal/privacy" in out


class TestNotifyTermsUpdateFailureResilience:
    """送信失敗があっても残りの対象に送信は継続 + 失敗件数が正しく集計される"""

    def test_send_continues_on_failure_and_counts(
        self, db, app, monkeypatch
    ):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        _make_user(db, "u1", "u1@example.com", None)
        _make_user(db, "u2", "u2@example.com", None)

        call_count = {"n": 0}

        from app.services import mail as mail_mod
        from app.services.mail import ConsoleMailBackend

        class IntermittentBackend(ConsoleMailBackend):
            def send(self, to, from_addr, rendered):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("simulated SMTP failure")
                super().send(to, from_addr, rendered)

        monkeypatch.setattr(
            mail_mod, "get_mail_backend", lambda: IntermittentBackend()
        )

        runner = app.test_cli_runner()
        result = runner.invoke(args=["notify-terms-update"])

        # 全件試行されること (失敗で打ち切られない)
        assert call_count["n"] == 2
        # 失敗 1 件 / 成功 1 件 が CLI 出力で集計される
        assert "成功 1 件" in result.output
        assert "失敗 1 件" in result.output
        # 失敗詳細 (ユーザー識別) が出力される
        assert "u1@example.com" in result.output
        assert "simulated SMTP failure" in result.output


class TestSendEmailRaiseOnError:
    """`send_email(raise_on_send_error=True)` で backend.send の例外を伝播"""

    def test_default_suppresses(self, app, monkeypatch):
        from app.services import mail as mail_mod
        from app.services.mail import ConsoleMailBackend, send_email

        class FailingBackend(ConsoleMailBackend):
            def send(self, to, from_addr, rendered):
                raise RuntimeError("boom")

        monkeypatch.setattr(mail_mod, "get_mail_backend", lambda: FailingBackend())
        with app.test_request_context():
            # raise しない (デフォルト)
            send_email("u@example.com", "_skeleton", {"body": "x"})

    def test_raise_when_flag_set(self, app, monkeypatch):
        import pytest
        from app.services import mail as mail_mod
        from app.services.mail import ConsoleMailBackend, send_email

        class FailingBackend(ConsoleMailBackend):
            def send(self, to, from_addr, rendered):
                raise RuntimeError("boom")

        monkeypatch.setattr(mail_mod, "get_mail_backend", lambda: FailingBackend())
        with app.test_request_context():
            with pytest.raises(RuntimeError, match="boom"):
                send_email(
                    "u@example.com", "_skeleton", {"body": "x"},
                    raise_on_send_error=True,
                )
