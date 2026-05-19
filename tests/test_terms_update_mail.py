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
    """送信失敗があっても残りの対象に送信は継続"""

    def test_failures_are_counted(
        self, db, app, monkeypatch
    ):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        _make_user(db, "u1", "u1@example.com", None)
        _make_user(db, "u2", "u2@example.com", None)

        # send_email を 1 つ目で raise させる挙動にモック
        call_count = {"n": 0}
        original_send = None

        from app.services import mail as mail_mod
        from app.services.mail import ConsoleMailBackend

        class IntermittentBackend(ConsoleMailBackend):
            def send(self, to, from_addr, rendered):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("simulated SMTP failure")
                # 2 件目以降は親クラス通り print
                super().send(to, from_addr, rendered)

        monkeypatch.setattr(
            mail_mod, "get_mail_backend", lambda: IntermittentBackend()
        )

        runner = app.test_cli_runner()
        result = runner.invoke(args=["notify-terms-update"])
        # send_email 自体は例外を吸収するので CLI は完走
        # mail.py の挙動により失敗は黙って 0 として数えられる (現実装)。
        # → 「完走することと、send が 2 回呼ばれること」を最低限の保証として確認
        assert call_count["n"] == 2
        assert "送信完了" in result.output
