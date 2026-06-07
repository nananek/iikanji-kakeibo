"""顧問ユーザー (auditor.py) のテスト"""

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

    def test_auditor_sees_grants(self, db, client, auditor, user):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=3,
            status="active",
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/auditor/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert user.username in body


class TestSwitch:
    def test_unauthenticated(self, client):
        resp = client.post("/auditor/switch/1")
        assert resp.status_code in (302, 401)

    def test_idor_other_auditor_grant(self, db, client, auditor, user):
        # 別の顧問ユーザーへの grant
        from app.models.user import User
        other_auditor = User(username="other_aud", email="o@x.com",
                              user_type="auditor")
        other_auditor.set_password("pw")
        db.session.add(other_auditor)
        db.session.commit()
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=other_auditor.id,
            permission_level=3,
            status="active",
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.post(f"/auditor/switch/{grant.id}")
        assert resp.status_code == 404

    def test_lv3_switch(self, db, client, auditor, user):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=3,
            status="active",
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.post(f"/auditor/switch/{grant.id}")
        assert resp.status_code in (302, 303)
        with client.session_transaction() as sess:
            assert sess["acting_as_user_id"] == user.id
            assert sess["acting_as_permission_level"] == 3

    def test_lv1_switch_redirects_to_tax(self, db, client, auditor, user):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=1,
            status="active",
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.post(f"/auditor/switch/{grant.id}")
        assert resp.status_code in (302, 303)
        assert "reports/tax" in resp.headers.get("Location", "") or \
               "tax" in resp.headers.get("Location", "")

    def test_lv2_unsubmitted_blocked(self, db, client, auditor, user):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=2,
            status="active",  # not submitted
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.post(f"/auditor/switch/{grant.id}")
        assert resp.status_code in (302, 303)
        # acting_as は設定されない
        with client.session_transaction() as sess:
            assert "acting_as_user_id" not in sess

    def test_lv2_submitted_allowed(self, db, client, auditor, user):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=2,
            status="submitted",
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.post(f"/auditor/switch/{grant.id}")
        assert resp.status_code in (302, 303)
        with client.session_transaction() as sess:
            assert sess.get("acting_as_user_id") == user.id


class TestExit:
    def test_unauthenticated(self, client):
        resp = client.post("/auditor/exit")
        assert resp.status_code in (302, 401)

    def test_clears_session(self, db, client, auditor, user):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 3
        resp = client.post("/auditor/exit")
        assert resp.status_code in (302, 303)
        with client.session_transaction() as sess:
            assert "acting_as_user_id" not in sess
            assert "acting_as_permission_level" not in sess
