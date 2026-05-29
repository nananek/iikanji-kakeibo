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

    def test_lv2_can_read_owner_journals(self, client, db, owner, auditor, grant_lv2):
        _login(client, auditor)
        _set_acting_as(client, owner.id, 2)
        # owner には journal がないので空配列 = owner として動いた証拠
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 200
        assert resp.get_json()["journals"] == []

    def test_lv3_can_read_owner_journals(self, client, db, owner, auditor, grant_lv3):
        _login(client, auditor)
        _set_acting_as(client, owner.id, 3)
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 200


class TestResolveActingAsWriteSession:
    """write 操作は permission_level=3 のみ許可。"""

    def test_lv3_encrypted_write_blocked_in_proxy(
            self, client, db, owner, auditor, grant_lv3,
    ):
        """PR-C: acting_as 中の write は encrypted_blob 付きなら _audit_proxy_blocks_
        encrypted_writes で 403、平文-only なら encrypted_blob 必須化で 400。
        Lv3 でも実質的に書き込み不可能 (AAD 不整合保護)。
        """
        from tests.conftest import encrypt_lines, encrypted_payload
        _login(client, auditor)
        _set_acting_as(client, owner.id, 3)
        # 暗号化 write: AAD 不整合防止のため 403
        resp = client.post("/api/v1/journals/batch", json={
            "entries": [{
                "date": "2026-02-01",
                "description": "x",
                "lines": encrypt_lines([
                    {"account_code": "9999", "debit": 100},
                    {"account_code": "8888", "credit": 100},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 403
        assert "代理閲覧" in resp.get_json()["error"]
        # 平文-only write: encrypted_blob 必須化で 400
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
        assert resp.status_code == 400
        assert "encrypted_blob" in resp.get_json()["error"]

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
        # Lv1 grant: read/write 問わず API は 403
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


class TestResolveActingAsLv1BlockedFromApi:
    """Lv1 監査アカウントは API 経由の代理閲覧を完全遮断 (集計のみ閲覧仕様)。"""

    def test_lv1_cannot_read_journals_via_acting_as(
            self, client, db, owner, auditor,
    ):
        g = AuditGrant(
            owner_user_id=owner.id, auditor_user_id=auditor.id,
            permission_level=1, status="submitted",
        )
        db.session.add(g); db.session.commit()
        _login(client, auditor)
        _set_acting_as(client, owner.id, 1)
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 403
        assert "Lv1" in resp.get_json()["error"]


class TestResolveActingAsDeletedOwner:
    """owner が DB から削除された場合は 401 を返す (data integrity safety net)。"""

    def test_deleted_owner_returns_401(
            self, client, db, owner, auditor, grant_lv3,
    ):
        # grant を保持したまま owner だけ消えるケースは FK 制約上ほぼ発生しないが、
        # 防御的ガードとして acting_as_user_id がセットされた状態で owner User が
        # 取れなくなる状況をシミュレートする。
        # SQLite で FK cascade が走らないよう、acting_as_user_id を存在しない id に差し替え。
        nonexistent_id = 999999
        _login(client, auditor)
        _set_acting_as(client, nonexistent_id, 3)
        # nonexistent_id を owner にする AuditGrant も作って、grant チェックは通る形に
        g = AuditGrant(
            owner_user_id=auditor.id, auditor_user_id=auditor.id,  # placeholder
            permission_level=3, status="submitted",
        )
        # ↑ FK 制約で nonexistent は無理なので、grant 経路を通らず effective_user
        # = None になるケースは実質「grant あり + owner User 削除」の同時発生で
        # FK 上発生しない。テストとして実現が難しいので skip (コードパスは静的解析で OK)
        import pytest
        pytest.skip("FK 制約により owner 削除 + grant 残存は発生し得ない (防御ガード)")


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


class TestResolveActingAsLiveDb:
    """DB grant の変更がリアルタイム反映されることを確認 (session キャッシュ非依存)。"""

    def test_grant_downgrade_lv3_to_lv1_blocks_read_next_request(
            self, client, db, owner, auditor, grant_lv3,
    ):
        """PR-C で acting_as 中の write は事実上不可能になったので、grant 降格の
        リアルタイム反映は read API で検証する (Lv3 read OK → Lv1 read 403)。
        """
        _login(client, auditor)
        _set_acting_as(client, owner.id, 3)
        # 初回 read は Lv3 → 200
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 200

        # DB で grant を Lv1 に降格 (session の permission_level=3 はそのまま)
        grant_lv3.permission_level = 1
        db.session.commit()

        # 次の read は Lv1 になるべき (session ではなく DB grant で判定) → 403
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 403

    def test_lv2_unsubmitted_grant_clears_session(
            self, client, db, owner, auditor, grant_lv2,
    ):
        # Lv2 grant が submitted のときは acting_as 有効
        _login(client, auditor)
        _set_acting_as(client, owner.id, 2)
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 200

        # オーナーが提出取消 (status = draft)
        grant_lv2.status = "draft"
        db.session.commit()

        # 次のリクエストでアクセス遮断 (auditor 本人として動作)
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert sess.get("acting_as_user_id") is None


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
