"""監査ユーザー (auditor.py) のテスト"""

from app.models.audit import AuditGrant


class TestDashboard:
    def test_unauthenticated(self, client):
        resp = client.get("/auditor/")
        assert resp.status_code in (302, 401)

    def test_personal_user_redirected_to_dashboard(self, logged_in_client):
        """個人ユーザーは /auditor/ にアクセスすると dashboard へリダイレクト"""
        resp = logged_in_client.get("/auditor/")
        assert resp.status_code in (302, 303)
        assert "/dashboard" in resp.headers.get("Location", "") or \
               resp.headers.get("Location", "").endswith("/")

    def test_auditor_sees_dashboard(self, db, client, auditor):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/auditor/")
        assert resp.status_code == 200

    def test_snapshot_workflow_banner_shown(self, db, client, auditor):
        """非同期スナップショット方式の案内バナーが表示される (§14.11)。

        旧リアルタイム代理閲覧は撤去済み (#112) のため「廃止予定」表記は無く、
        スナップショット方式の案内のみを表示する。
        """
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/auditor/")
        body = resp.get_data(as_text=True)
        assert "非同期スナップショット方式" in body

    def test_auditor_sees_grants(self, db, client, auditor, user):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=3,
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/auditor/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert user.username in body


class TestPackages:
    def test_unauthenticated(self, client):
        resp = client.get("/auditor/packages/1")
        assert resp.status_code in (302, 401)

    def test_auditor_sees_packages_page(self, db, client, auditor, user):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=2,
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get(f"/auditor/packages/{grant.id}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert user.username in body
        # クライアント側で復号する設定 island が描画される
        assert "audit-review-config" in body
        assert "audit_review_renderer.mjs" in body
        # PR-B: 修正案送信フォームと owner 公開鍵 fingerprint widget が描画される
        assert "audit-compose" in body
        assert 'data-peer-role="OWNER"' in body
        assert "initAuditCompose" in body

    def test_personal_user_redirected(self, db, client, auditor, user):
        """個人ユーザーは /auditor/packages にアクセスすると dashboard へリダイレクト"""
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=2,
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)  # personal ユーザー
        resp = client.get(f"/auditor/packages/{grant.id}")
        assert resp.status_code in (302, 303)

    def test_revoked_grant_redirected(self, db, client, auditor, user):
        """失効した監査アクセスは packages 閲覧でリダイレクト (§14.10)"""
        from datetime import datetime, timezone
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=2,
            revoked_at=datetime.now(timezone.utc),
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get(f"/auditor/packages/{grant.id}")
        assert resp.status_code in (302, 303)

    def test_idor_other_auditor_grant(self, db, client, auditor, user):
        from app.models.user import User
        other_auditor = User(username="other_aud2", email="o2@x.com",
                             user_type="auditor")
        other_auditor.set_password("pw")
        db.session.add(other_auditor)
        db.session.commit()
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=other_auditor.id,
            permission_level=3,
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get(f"/auditor/packages/{grant.id}")
        assert resp.status_code == 404


class TestProxyRoutesRemoved:
    """旧リアルタイム代理閲覧ルート (switch/exit) は撤去済み (#112)。"""

    def test_switch_route_gone(self, client):
        resp = client.post("/auditor/switch/1")
        assert resp.status_code == 404

    def test_exit_route_gone(self, client):
        resp = client.post("/auditor/exit")
        assert resp.status_code == 404
