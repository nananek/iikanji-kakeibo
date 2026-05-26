"""resolve_bearer_or_session の acting_as_user_id 対応 (Phase E3-F-1) のテスト。

Bearer 認証時は acting_as を見ない (API 専用)。
セッション認証 + acting_as_user_id がある時:
  - 有効なグラントがあれば effective_user (= owner) を返す
  - write 操作は permission_level=3 のみ許可
  - グラント取消済ならセッションクリアして auditor 本人として動作
"""

import pytest

from app.extensions import db
from app.models.audit import AuditGrant
from app.models.user import User


def _login(client, user):
    """Flask-Login session に user をセット (auditor として login)。"""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _set_acting_as(client, owner_id, permission_level):
    with client.session_transaction() as sess:
        sess["acting_as_user_id"] = owner_id
        sess["acting_as_permission_level"] = permission_level


@pytest.fixture
def owner(db):
    u = User(username="owner", email="o@example.com")
    u.set_password("pw")
    db.session.add(u); db.session.commit()
    return u


@pytest.fixture
def auditor(db):
    u = User(username="auditor", email="a@example.com", user_type="auditor")
    u.set_password("pw")
    db.session.add(u); db.session.commit()
    return u


@pytest.fixture
def grant_lv3(db, owner, auditor):
    g = AuditGrant(
        owner_user_id=owner.id, auditor_user_id=auditor.id,
        permission_level=3, status="submitted",
    )
    db.session.add(g); db.session.commit()
    return g


@pytest.fixture
def grant_lv2(db, owner, auditor):
    g = AuditGrant(
        owner_user_id=owner.id, auditor_user_id=auditor.id,
        permission_level=2, status="submitted",
    )
    db.session.add(g); db.session.commit()
    return g


class TestResolveActingAsReadSession:
    """セッション認証 + acting_as 中の read 操作は permission_level 問わず通る。"""

    def test_lv2_can_read_owner_journals(self, client, db, owner, auditor, grant_lv2, accounts):
        # accounts fixture は owner ではなく user に紐づくが、ここでは API がエラーを
        # 返さず owner の id でフィルタする (= 空 list) ことを検証。
        _login(client, auditor)
        _set_acting_as(client, owner.id, 2)
        # GET /api/v1/journals (read) は scope: journals:read 必要だが、セッション認証は
        # scope 不問。allow_session=True で動く想定。
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 200
        # owner には journal がないので空配列
        assert resp.get_json()["journals"] == []

    def test_lv3_can_read_owner_journals(self, client, db, owner, auditor, grant_lv3, accounts):
        _login(client, auditor)
        _set_acting_as(client, owner.id, 3)
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 200


class TestResolveActingAsWriteSession:
    """write 操作は permission_level=3 のみ許可。"""

    def test_lv3_can_create_journal_for_owner(
            self, client, db, owner, auditor, grant_lv3,
    ):
        # owner には科目を seed しないので validate で 400 (account_code 不存在)
        # で返るはず = 認証が通って owner として動いた証拠 (Lv3 で write 許可)
        _login(client, auditor)
        _set_acting_as(client, owner.id, 3)
        resp = client.post("/api/v1/journals/batch", json={
            "entries": [{
                "date": "2026-02-01",
                "description": "x",
                "lines": [
                    {"account_code": "9999", "debit": 100},
                    {"account_code": "8888", "credit": 100},
                ],
            }],
        })
        # 認証は通り、validate で 400 (科目が存在しない)
        assert resp.status_code == 400
        err = resp.get_json()["error"]
        assert "9999" in err or "8888" in err

    def test_lv2_cannot_create_journal(
            self, client, db, owner, auditor, grant_lv2,
    ):
        _login(client, auditor)
        _set_acting_as(client, owner.id, 2)
        resp = client.post("/api/v1/journals/batch", json={
            "entries": [{
                "date": "2026-02-01", "description": "x",
                "lines": [
                    {"account_code": "1010", "debit": 100},
                    {"account_code": "5010", "credit": 100},
                ],
            }],
        })
        assert resp.status_code == 403
        assert "Lv3" in resp.get_json()["error"] or "権限レベル" in resp.get_json()["error"]

    def test_lv1_cannot_create_journal(
            self, client, db, owner, auditor,
    ):
        # Lv1 grant
        g = AuditGrant(
            owner_user_id=owner.id, auditor_user_id=auditor.id,
            permission_level=1, status="submitted",
        )
        db.session.add(g); db.session.commit()
        _login(client, auditor)
        _set_acting_as(client, owner.id, 1)
        resp = client.post("/api/v1/journals/batch", json={
            "entries": [{
                "date": "2026-02-01", "description": "x",
                "lines": [
                    {"account_code": "1010", "debit": 100},
                    {"account_code": "5010", "credit": 100},
                ],
            }],
        })
        assert resp.status_code == 403


class TestResolveActingAsRevoked:
    """グラント取消後にセッションだけ残っている場合、auditor 本人として動作する。"""

    def test_revoked_grant_falls_back_to_auditor(
            self, client, db, owner, auditor,
    ):
        # グラントは作らない (= acting_as が無効)
        _login(client, auditor)
        _set_acting_as(client, owner.id, 3)
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 200
        # auditor 本人の journal (= 空) が返る
        assert resp.get_json()["journals"] == []
        # セッションがクリアされている
        with client.session_transaction() as sess:
            assert sess.get("acting_as_user_id") is None
            assert sess.get("acting_as_permission_level") is None


class TestResolveBearerIgnoresActingAs:
    """Bearer 認証では acting_as_user_id を無視する (API 専用ルート)。"""

    def test_api_key_does_not_use_acting_as(
            self, client, db, user, accounts, auth_header, owner, grant_lv3,
    ):
        # 仮にセッションに acting_as があっても、API キーは current_user (= user) を使う
        with client.session_transaction() as sess:
            sess["acting_as_user_id"] = owner.id
            sess["acting_as_permission_level"] = 3
        resp = client.get("/api/v1/journals?fiscal_year=2026", headers=auth_header)
        assert resp.status_code == 200
        # auth_header は user のキー、acting_as は無視 → user の journals が返る
        assert resp.get_json()["journals"] == []
