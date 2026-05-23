"""E1 PR-D: flask rotate-cleanup CLI のテスト。

設計書 §10.5 / §16.4: auto_abort_at 経過のローテーションを自動 abort。
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.extensions import limiter
from app.models import User, WrappedKey
from app.models.wrapped_key import METHOD_PASSPHRASE, METHOD_RECOVERY_SEED


@pytest.fixture(autouse=True)
def _reset_rate_limits(app):
    with app.app_context():
        try:
            limiter.reset()
        except Exception:
            pass
    yield


def _make_user(db, username=None):
    username = username or f"u{uuid4().hex[:8]}"
    u = User(username=username, email=f"{username}@example.com")
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u


def _start_rotation(user, deadline: datetime, new_ids: list[int] | None = None):
    """user の mk_rotation_state を rotating + auto_abort_at で初期化。"""
    user.mk_rotation_state = {
        "status": "rotating",
        "started_at": (deadline - timedelta(days=7)).isoformat(),
        "rotation_token_hash": "0" * 64,
        "auto_abort_at": deadline.isoformat(),
        "new_wrapped_keys_id_set": new_ids or [],
    }


def _make_wrapped_key(db, user, method=METHOD_PASSPHRASE):
    if method == METHOD_PASSPHRASE:
        row = WrappedKey(
            user_id=user.id, method=method,
            wrapped_master_key=b"\x00" * 48, wrap_iv=b"\x01" * 12,
            salt=b"\x02" * 16,
            kdf_params={"memory": 65536, "iterations": 3, "parallelism": 1},
        )
    else:
        row = WrappedKey(
            user_id=user.id, method=method,
            wrapped_master_key=b"\x10" * 48, wrap_iv=b"\x11" * 12,
        )
    db.session.add(row)
    db.session.commit()
    return row


def test_cleanup_aborts_expired_rotation(db, app):
    """期限切れの rotation を abort し、new_wrapped_keys を削除する。"""
    user = _make_user(db)
    old = _make_wrapped_key(db, user, METHOD_PASSPHRASE)
    new = _make_wrapped_key(db, user, METHOD_RECOVERY_SEED)
    # 1 日前に期限切れ
    deadline = datetime.now(timezone.utc) - timedelta(days=1)
    _start_rotation(user, deadline, new_ids=[new.id])
    db.session.commit()

    runner = app.test_cli_runner()
    result = runner.invoke(args=["rotate-cleanup"])
    assert result.exit_code == 0
    assert "aborted=1" in result.output
    assert "deleted_wrapped_keys=1" in result.output

    db.session.expire_all()
    user_refreshed = db.session.get(User, user.id)
    assert user_refreshed.mk_rotation_state is None
    # new は削除、old は残る
    rows = WrappedKey.query.filter_by(user_id=user.id).all()
    ids = sorted(r.id for r in rows)
    assert ids == [old.id]


def test_cleanup_skips_not_yet_expired(db, app):
    """期限内のローテーションは触らない。"""
    user = _make_user(db)
    _make_wrapped_key(db, user)
    deadline = datetime.now(timezone.utc) + timedelta(days=3)
    _start_rotation(user, deadline)
    db.session.commit()

    runner = app.test_cli_runner()
    result = runner.invoke(args=["rotate-cleanup"])
    assert result.exit_code == 0
    assert "aborted=0" in result.output
    assert "still_in_window=1" in result.output

    db.session.expire_all()
    user_refreshed = db.session.get(User, user.id)
    assert user_refreshed.mk_rotation_state is not None


def test_cleanup_dry_run_does_not_modify(db, app):
    """--dry-run で対象を表示するが何も変更しない。"""
    user = _make_user(db)
    new = _make_wrapped_key(db, user)
    deadline = datetime.now(timezone.utc) - timedelta(days=1)
    _start_rotation(user, deadline, new_ids=[new.id])
    db.session.commit()

    runner = app.test_cli_runner()
    result = runner.invoke(args=["rotate-cleanup", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run]" in result.output

    db.session.expire_all()
    user_refreshed = db.session.get(User, user.id)
    # state は維持されたまま
    assert user_refreshed.mk_rotation_state is not None


def test_cleanup_handles_no_rotations(db, app):
    """rotation 中ユーザーがいない場合も正常終了。"""
    _make_user(db)
    runner = app.test_cli_runner()
    result = runner.invoke(args=["rotate-cleanup"])
    assert result.exit_code == 0
    assert "aborted=0" in result.output


def test_cleanup_handles_invalid_deadline(db, app):
    """auto_abort_at が壊れている場合はスキップ。"""
    user = _make_user(db)
    user.mk_rotation_state = {
        "status": "rotating",
        "auto_abort_at": "not-a-date",
        "new_wrapped_keys_id_set": [],
    }
    db.session.commit()
    runner = app.test_cli_runner()
    result = runner.invoke(args=["rotate-cleanup"])
    assert result.exit_code == 0
    assert "aborted=0" in result.output
