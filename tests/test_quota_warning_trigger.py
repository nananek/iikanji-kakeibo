"""Phase 6 #71: quota_warning メール送信トリガー."""

from unittest.mock import patch

import pytest

from app.models.storage import StorageUsage
from app.services.storage_quota import maybe_send_quota_warning


MB = 1024 * 1024


def _set_usage(db, user, used_bytes):
    """既存 row があれば更新、なければ作成 (テストヘルパー)."""
    row = db.session.get(StorageUsage, user.id)
    if row is None:
        db.session.add(StorageUsage(user_id=user.id, used_bytes=used_bytes))
    else:
        row.used_bytes = used_bytes
    db.session.commit()


class TestThresholdDetection:
    """80% / 95% 閾値の到達検出."""

    def test_below_70_pct_no_email(self, app, db, user):
        with app.test_request_context():
            _set_usage(db, user, 100 * MB)  # 20%
            sent = []
            with patch(
                "app.services.storage_quota.send_email",
                side_effect=lambda to, t, ctx=None, **kw: sent.append(
                    (to, t, ctx)
                ),
            ):
                maybe_send_quota_warning(user)
            assert sent == []
            assert user.last_quota_warning_level is None

    def test_70_to_80_hysteresis_no_email(self, app, db, user):
        """70-80% のヒステリシス区間ではメール送信なし、state 維持."""
        with app.test_request_context():
            _set_usage(db, user, 375 * MB)  # 75%
            sent = []
            with patch(
                "app.services.storage_quota.send_email",
                side_effect=lambda *a, **kw: sent.append(a),
            ):
                maybe_send_quota_warning(user)
            assert sent == []
            assert user.last_quota_warning_level is None

    def test_80_pct_sends_warning(self, app, db, user):
        """80% 到達で warning メール送信."""
        with app.test_request_context():
            _set_usage(db, user, 400 * MB)  # 80%
            sent = []
            with patch(
                "app.services.storage_quota.send_email",
                side_effect=lambda to, t, ctx=None, **kw: sent.append(
                    (to, t, ctx)
                ),
            ):
                maybe_send_quota_warning(user)
            assert len(sent) == 1
            assert sent[0][0] == user.email
            assert sent[0][1] == "quota_warning"
            assert sent[0][2]["level"] == "warning"
            assert sent[0][2]["percentage"] == 80.0
            assert user.last_quota_warning_level == "warning"

    def test_95_pct_sends_critical(self, app, db, user):
        """95% 到達で critical メール送信."""
        with app.test_request_context():
            _set_usage(db, user, 475 * MB)  # 95%
            sent = []
            with patch(
                "app.services.storage_quota.send_email",
                side_effect=lambda to, t, ctx=None, **kw: sent.append(
                    (to, t, ctx)
                ),
            ):
                maybe_send_quota_warning(user)
            assert len(sent) == 1
            assert sent[0][2]["level"] == "critical"
            assert user.last_quota_warning_level == "critical"


class TestDuplicateSuppression:
    """重複通知防止."""

    def test_same_warning_level_no_resend(self, app, db, user):
        """既に warning 通知済の場合、80%-95% 範囲内では再送しない."""
        with app.test_request_context():
            _set_usage(db, user, 400 * MB)
            user.last_quota_warning_level = "warning"
            db.session.commit()

            sent = []
            with patch(
                "app.services.storage_quota.send_email",
                side_effect=lambda *a, **kw: sent.append(a),
            ):
                maybe_send_quota_warning(user)
            assert sent == []

    def test_warning_to_critical_upgrades(self, app, db, user):
        """warning → critical (95% 到達) は新規通知."""
        with app.test_request_context():
            user.last_quota_warning_level = "warning"
            _set_usage(db, user, 475 * MB)
            db.session.commit()

            sent = []
            with patch(
                "app.services.storage_quota.send_email",
                side_effect=lambda to, t, ctx=None, **kw: sent.append(
                    (to, t, ctx)
                ),
            ):
                maybe_send_quota_warning(user)
            assert len(sent) == 1
            assert sent[0][2]["level"] == "critical"
            assert user.last_quota_warning_level == "critical"

    def test_critical_to_warning_no_downgrade_notification(
        self, app, db, user,
    ):
        """critical → warning (95% → 80% 帯) への戻り通知はしない."""
        with app.test_request_context():
            user.last_quota_warning_level = "critical"
            _set_usage(db, user, 400 * MB)  # 80%
            db.session.commit()

            sent = []
            with patch(
                "app.services.storage_quota.send_email",
                side_effect=lambda *a, **kw: sent.append(a),
            ):
                maybe_send_quota_warning(user)
            assert sent == []
            # state は critical のまま (downgrade しない)
            assert user.last_quota_warning_level == "critical"


class TestReset:
    """ヒステリシス: 70% 未満まで回復したら state リセット."""

    def test_below_70_resets_state(self, app, db, user):
        with app.test_request_context():
            user.last_quota_warning_level = "warning"
            _set_usage(db, user, 100 * MB)  # 20%
            db.session.commit()

            maybe_send_quota_warning(user)
            assert user.last_quota_warning_level is None

    def test_reset_then_warn_again(self, app, db, user):
        """リセット後に再び 80% 到達でメール再送できる."""
        with app.test_request_context():
            user.last_quota_warning_level = "warning"
            _set_usage(db, user, 100 * MB)  # 20%
            db.session.commit()
            maybe_send_quota_warning(user)  # リセット
            assert user.last_quota_warning_level is None

            _set_usage(db, user, 400 * MB)  # 80% 再到達
            sent = []
            with patch(
                "app.services.storage_quota.send_email",
                side_effect=lambda to, t, ctx=None, **kw: sent.append(
                    (to, t, ctx)
                ),
            ):
                maybe_send_quota_warning(user)
            assert len(sent) == 1
            assert sent[0][2]["level"] == "warning"


class TestFailureHandling:
    """send_email 失敗時もユーザー操作を妨げない."""

    def test_send_email_exception_swallowed(self, app, db, user):
        with app.test_request_context():
            _set_usage(db, user, 400 * MB)

            def raise_error(*args, **kwargs):
                raise RuntimeError("SMTP down")

            with patch(
                "app.services.storage_quota.send_email",
                side_effect=raise_error,
            ):
                # 例外を握って関数は正常終了する
                maybe_send_quota_warning(user)
            # state は更新済 (commit が send_email 前に行われる設計のため
            # send_email 失敗時もこの値は残る)。次回は再送されないが、
            # 次回送信は admin 側のログから手動再試行する想定。
            assert user.last_quota_warning_level == "warning"

    def test_zero_quota_no_division_error(
        self, app, db, user, monkeypatch,
    ):
        with app.test_request_context():
            monkeypatch.setitem(app.config, "STORAGE_QUOTA_BYTES_DEFAULT", 0)
            _set_usage(db, user, 100)
            sent = []
            with patch(
                "app.services.storage_quota.send_email",
                side_effect=lambda *a, **kw: sent.append(a),
            ):
                # quota=0 でも division エラーにならず正常終了
                maybe_send_quota_warning(user)
            assert sent == []
