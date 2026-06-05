"""E7 (#114) §16.5: flask migration-purge-locked CLI のテスト。

ロック後 60 日経過した鍵未設定ユーザーを自動退会 (delete_user_account)。
不可逆のため既定は dry-run、--execute 明示時のみ削除する。
"""

import json
from datetime import datetime, timedelta, timezone

from app.models.user import User


def _user(db, name, *, public_key=None, active=True, locked_days_ago=None,
          user_type="personal"):
    u = User(username=name, email=f"{name}@e.com", user_type=user_type)
    u.set_password("pw")
    u.public_key = public_key
    u.is_active = active
    if locked_days_ago is not None:
        u.locked_at = datetime.now(timezone.utc) - timedelta(days=locked_days_ago)
    db.session.add(u)
    db.session.commit()
    return u


def _last_json(output):
    return json.loads(output.strip().splitlines()[-1])


def test_dry_run_lists_but_does_not_delete(db, app):
    u = _user(db, "stale", public_key=None, active=False, locked_days_ago=70)
    result = app.test_cli_runner().invoke(args=["migration-purge-locked", "--json"])
    assert result.exit_code == 0
    assert "[dry-run]" in result.output
    payload = _last_json(result.output)
    assert payload == {"matched": 1, "purged": 0, "failed": 0, "executed": False}
    # 削除されていない
    assert db.session.get(User, u.id) is not None


def test_execute_purges_long_locked_user(db, app):
    u = _user(db, "stale", public_key=None, active=False, locked_days_ago=70)
    uid = u.id
    result = app.test_cli_runner().invoke(
        args=["migration-purge-locked", "--execute", "--json"])
    assert result.exit_code == 0
    payload = _last_json(result.output)
    assert payload["matched"] == 1
    assert payload["purged"] == 1
    assert payload["executed"] is True
    assert db.session.get(User, uid) is None


def test_recently_locked_user_excluded(db, app):
    """ロックから 60 日未満は対象外。"""
    u = _user(db, "recent", public_key=None, active=False, locked_days_ago=10)
    result = app.test_cli_runner().invoke(
        args=["migration-purge-locked", "--execute", "--json"])
    assert _last_json(result.output)["matched"] == 0
    assert db.session.get(User, u.id) is not None


def test_scope_excludes_non_targets(db, app):
    # 鍵設定済み (ロック中でも対象外: public_key あり)
    keyset = _user(db, "keyset", public_key=b"k", active=False, locked_days_ago=70)
    # locked_at なし (ロックされていない / 旧データ)
    no_lock = _user(db, "no_lock", public_key=None, active=False, locked_days_ago=None)
    # is_active=True (ロックされていない)
    active = _user(db, "active", public_key=None, active=True, locked_days_ago=70)
    # 監査アカウント
    auditor = _user(db, "aud", public_key=None, active=False, locked_days_ago=70,
                    user_type="auditor")
    # 真の対象
    target = _user(db, "target", public_key=None, active=False, locked_days_ago=70)

    result = app.test_cli_runner().invoke(
        args=["migration-purge-locked", "--execute", "--json"])
    assert result.exit_code == 0
    assert _last_json(result.output)["purged"] == 1

    assert db.session.get(User, keyset.id) is not None
    assert db.session.get(User, no_lock.id) is not None
    assert db.session.get(User, active.id) is not None
    assert db.session.get(User, auditor.id) is not None
    assert db.session.get(User, target.id) is None


def test_empty_db(db, app):
    result = app.test_cli_runner().invoke(
        args=["migration-purge-locked", "--execute", "--json"])
    assert result.exit_code == 0
    assert _last_json(result.output) == {
        "matched": 0, "purged": 0, "failed": 0, "executed": True}
