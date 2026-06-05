"""E7 (#114) §16.6: flask migration-status CLI のテスト。

personal ユーザーの鍵設定/ temp-MK 保持状況を集計表示する。read のみ。
"""

import json

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


def test_migration_status_counts(db, app):
    # 鍵設定済み & temp-MK あり (移行待ち)
    _user(db, "a", public_key=b"k", temp_mk=bytes(32))
    # 鍵設定済み & temp-MK なし (移行完了)
    _user(db, "b", public_key=b"k", temp_mk=None)
    # 鍵未設定 & ロック中
    _user(db, "c", public_key=None, temp_mk=None, active=False)
    # 監査アカウントは集計対象外
    _user(db, "auditor1", user_type="auditor")

    result = app.test_cli_runner().invoke(args=["migration-status"])
    assert result.exit_code == 0
    out = result.output
    assert "総ユーザー数            : 3" in out  # personal のみ
    assert "鍵設定済み              : 2" in out
    assert "鍵未設定                : 1" in out
    assert "temp-MK 保持中(移行待ち) : 1" in out
    assert "ロック中(is_active=False): 1" in out
    # 移行待ちが残るので破棄不可メッセージ
    assert "移行待ち] 1 名" in out


def test_migration_status_safe_to_discard_when_no_temp_mk(db, app):
    _user(db, "a", public_key=b"k", temp_mk=None)
    _user(db, "b", public_key=b"k", temp_mk=None)

    result = app.test_cli_runner().invoke(args=["migration-status"])
    assert result.exit_code == 0
    assert "移行完遂]" in result.output
    assert "temp-MK 材料を破棄できます" in result.output


def test_migration_status_json(db, app):
    _user(db, "a", public_key=b"k", temp_mk=bytes(32))
    _user(db, "b", public_key=None, temp_mk=None)

    result = app.test_cli_runner().invoke(args=["migration-status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {
        "total": 2,
        "key_set": 1,
        "key_unset": 1,
        "temp_mk_active": 1,
        "locked": 0,
        "safe_to_discard_temp_mk": False,
    }


def test_migration_status_empty(db, app):
    result = app.test_cli_runner().invoke(args=["migration-status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["total"] == 0
    # 誰も temp-MK を持たない → 破棄可能 (true)
    assert data["safe_to_discard_temp_mk"] is True
