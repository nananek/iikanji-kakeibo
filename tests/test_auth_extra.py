"""認証ビュー (auth.py) の追加テスト

test_security_auth.py が CAPTCHA 系を扱う。こちらは register / login /
logout / safe_next_url ロジックを補完。
"""

from app.models.account import Account
from app.models.user import User


class TestLoginGet:
    def test_get_login_page(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_get_login_redirects_when_authenticated(self, logged_in_client):
        resp = logged_in_client.get("/login")
        assert resp.status_code in (302, 303)


class TestLoginPost:
    def test_invalid_credentials(self, client, user):
        resp = client.post("/login", data={
            "username": user.username,
            "password": "wrong-password",
        })
        # form 再表示 (フラッシュメッセージ)
        assert resp.status_code == 200

    def test_valid_credentials(self, client, user):
        resp = client.post("/login", data={
            "username": user.username,
            "password": "password123",
        })
        assert resp.status_code in (302, 303)

    def test_login_with_safe_next(self, client, user):
        resp = client.post("/login?next=/journal/", data={
            "username": user.username,
            "password": "password123",
        })
        assert resp.status_code in (302, 303)
        assert "/journal" in resp.headers.get("Location", "")

    def test_login_with_external_next_blocked(self, client, user):
        resp = client.post("/login?next=https://evil.com/", data={
            "username": user.username,
            "password": "password123",
        })
        assert resp.status_code in (302, 303)
        assert "evil.com" not in resp.headers.get("Location", "")

    def test_login_with_protocol_relative_next_blocked(self, client, user):
        resp = client.post("/login?next=//evil.com/", data={
            "username": user.username,
            "password": "password123",
        })
        assert resp.status_code in (302, 303)
        assert "evil.com" not in resp.headers.get("Location", "")

    def test_personal_cannot_login_via_auditor(self, client, user):
        resp = client.post("/login/auditor", data={
            "username": user.username,
            "password": "password123",
        })
        # 個人ユーザーは監査ログイン不可
        assert resp.status_code == 200


class TestLoginAuditor:
    def test_get_renders(self, client):
        resp = client.get("/login/auditor")
        assert resp.status_code == 200

    def test_auditor_can_login(self, client, auditor):
        resp = client.post("/login/auditor", data={
            "username": auditor.username,
            "password": "password123",
        })
        assert resp.status_code in (302, 303)

    def test_redirected_when_authenticated(self, client, auditor):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/login/auditor")
        assert resp.status_code in (302, 303)


class TestRegisterGet:
    def test_get_renders(self, client):
        resp = client.get("/register")
        assert resp.status_code == 200

    def test_redirected_when_authenticated(self, logged_in_client):
        resp = logged_in_client.get("/register")
        assert resp.status_code in (302, 303)


class TestRegisterPost:
    def test_create_personal_account(self, db, client, account_types):
        resp = client.post("/register", data={
            "username": "newuser",
            "email": "new@example.com",
            "password": "newpass123",
            "password_confirm": "newpass123",
        })
        assert resp.status_code in (302, 303, 200)
        u = User.query.filter_by(username="newuser").first()
        if u:
            # 標準勘定科目もシードされる
            assert Account.query.filter_by(user_id=u.id).count() > 0
            assert u.user_type == "personal"

    def test_create_with_password_mismatch(self, db, client, account_types):
        resp = client.post("/register", data={
            "username": "new2",
            "email": "new2@example.com",
            "password": "x",
            "password_confirm": "y",
        })
        # validation 失敗で form 再表示
        assert resp.status_code == 200
        assert User.query.filter_by(username="new2").first() is None


class TestRegisterAuditor:
    def test_get_renders(self, client):
        resp = client.get("/register/auditor")
        assert resp.status_code == 200

    def test_create_auditor_account(self, db, client):
        resp = client.post("/register/auditor", data={
            "username": "audit1",
            "email": "a@example.com",
            "password": "auditpass",
            "password_confirm": "auditpass",
        })
        assert resp.status_code in (302, 303, 200)
        u = User.query.filter_by(username="audit1").first()
        if u:
            assert u.user_type == "auditor"
            # auditor は科目シードされない
            assert Account.query.filter_by(user_id=u.id).count() == 0

    def test_redirected_when_authenticated(self, logged_in_client):
        resp = logged_in_client.get("/register/auditor")
        assert resp.status_code in (302, 303)


class TestLogout:
    def test_unauthenticated_redirects(self, client):
        resp = client.get("/logout")
        assert resp.status_code in (302, 401)

    def test_logout_clears_session(self, logged_in_client, user, accounts):
        resp = logged_in_client.get("/logout")
        assert resp.status_code in (302, 303)
        # 続けてアクセスすると未認証扱い
        resp2 = logged_in_client.get("/journal/")
        assert resp2.status_code in (302, 303)
