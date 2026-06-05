"""E7 (#114) §16.5: flask migration-lock-stale CLI のテスト。

メンテナンスウィンドウ (config.MIGRATION_WINDOW_DATE) から猶予 30 日を過ぎても
鍵未設定の移行対象ユーザーをロックする。既定は dry-run、--execute で適用。
"""

import json

import pytest

from app.models.user import User


def _user(db, name, *, public_key=None, temp_mk=None, active=True,
          user_type="personal"):
    u = User(username=name, email=f"{name}@e.com", user_type=user_type)
    u.set_password("pw")
    u.public_key = public_key
    u.migration_temp_mk = temp_mk
    u.is_active = active
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def window_past(app, monkeypatch):
    """猶予を過ぎた基準日 (ロック対象が発生する)。"""
    monkeypatch.setitem(app.config, "MIGRATION_WINDOW_DATE", "2020-01-01")
    return app


@pytest.fixture
def window_future(app, monkeypatch):
    """まだ猶予期間中の基準日。"""
    monkeypatch.setitem(app.config, "MIGRATION_WINDOW_DATE", "2099-01-01")
    return app


def test_noop_when_window_unset(db, app, monkeypatch):
    monkeypatch.setitem(app.config, "MIGRATION_WINDOW_DATE", "")
    _user(db, "a", temp_mk=bytes(32))
    result = app.test_cli_runner().invoke(args=["migration-lock-stale", "--execute"])
    assert result.exit_code == 0
    assert "スキップ" in result.output
    # window 未設定なので何もロックされない (no-op)。
    assert User.query.filter(User.is_active.is_(False)).count() == 0


def test_invalid_window_format(db, app, monkeypatch):
    monkeypatch.setitem(app.config, "MIGRATION_WINDOW_DATE", "2020/01/01")
    result = app.test_cli_runner().invoke(args=["migration-lock-stale"])
    assert result.exit_code == 0
    assert "形式が不正" in result.output


def test_in_grace_period_no_targets(db, window_future):
    _user(db, "a", temp_mk=bytes(32))
    result = window_future.test_cli_runner().invoke(
        args=["migration-lock-stale", "--json"])
    assert result.exit_code == 0
    assert "猶予期間中" in result.output
    assert User.query.filter(User.is_active.is_(False)).count() == 0


def test_dry_run_lists_but_does_not_lock(db, window_past):
    _user(db, "stale", public_key=None, temp_mk=bytes(32))
    result = window_past.test_cli_runner().invoke(args=["migration-lock-stale"])
    assert result.exit_code == 0
    assert "[dry-run]" in result.output
    assert "stale" in result.output
    # dry-run では変更なし
    assert User.query.filter_by(username="stale").first().is_active is True


def test_execute_locks_stale_user(db, window_past):
    u = _user(db, "stale", public_key=None, temp_mk=bytes(32))
    result = window_past.test_cli_runner().invoke(
        args=["migration-lock-stale", "--execute"])
    assert result.exit_code == 0
    refreshed = db.session.get(User, u.id)
    assert refreshed.is_active is False
    assert refreshed.locked_at is not None


def test_execute_scope_excludes_non_targets(db, window_past):
    keyset = _user(db, "keyset", public_key=b"k", temp_mk=bytes(32))
    no_temp = _user(db, "no_temp", public_key=None, temp_mk=None)
    auditor = _user(db, "aud", temp_mk=bytes(32), user_type="auditor")
    already = _user(db, "already", public_key=None, temp_mk=bytes(32), active=False)
    target = _user(db, "target", public_key=None, temp_mk=bytes(32))

    result = window_past.test_cli_runner().invoke(
        args=["migration-lock-stale", "--execute", "--json"])
    assert result.exit_code == 0

    # 鍵設定済み・temp-MK なし・監査・既ロックは対象外、target のみ新規ロック。
    assert db.session.get(User, keyset.id).is_active is True
    assert db.session.get(User, no_temp.id).is_active is True
    assert db.session.get(User, auditor.id).is_active is True
    assert db.session.get(User, target.id).is_active is False
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["locked"] == 1
    assert payload["executed"] is True


def test_empty_db(db, window_past):
    result = window_past.test_cli_runner().invoke(
        args=["migration-lock-stale", "--execute", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["locked"] == 0
