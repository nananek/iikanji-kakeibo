"""認証フロー・セッション保護のセキュリティテスト"""

import pytest

from app.models.user import User


def _has_flash(resp, text):
    """flash メッセージ（showToast 内の tojson 出力）にテキストが含まれるか"""
    return text in resp.data.decode()


class TestLogin:
    """POST /login — 個人ユーザーログイン"""

    def test_login_page_renders(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_login_success(self, client, db, user):
        resp = client.post("/login", data={
            "username": "testuser",
            "password": "password123",
        }, follow_redirects=False)
        assert resp.status_code == 302
        # ダッシュボードへリダイレクト
        assert resp.headers["Location"] in ("/", "/dashboard", "/dashboard/")

    def test_login_wrong_password_not_redirect(self, client, db, user):
        """パスワード誤り → ログインせずフォームを再表示"""
        resp = client.post("/login", data={
            "username": "testuser",
            "password": "wrongpassword",
        }, follow_redirects=False)
        # ログイン成功 (302) ではない
        assert resp.status_code == 200

    def test_login_nonexistent_user_not_redirect(self, client, db):
        """存在しないユーザー → ログインせずフォームを再表示"""
        resp = client.post("/login", data={
            "username": "nouser",
            "password": "whatever",
        }, follow_redirects=False)
        assert resp.status_code == 200

    def test_same_response_for_wrong_user_and_wrong_password(self, client, db, user):
        """ユーザー列挙を防ぐため、存在/不在で同じレスポンス（200）"""
        resp_no_user = client.post("/login", data={
            "username": "nouser", "password": "whatever",
        }, follow_redirects=False)
        resp_wrong_pw = client.post("/login", data={
            "username": "testuser", "password": "wrongpassword",
        }, follow_redirects=False)
        assert resp_no_user.status_code == resp_wrong_pw.status_code == 200

    def test_auditor_cannot_use_personal_login(self, client, db, auditor):
        """監査ユーザーは /login からログインできない"""
        resp = client.post("/login", data={
            "username": "auditor",
            "password": "password123",
        }, follow_redirects=False)
        # ログイン成功 (302) ではない
        assert resp.status_code == 200

    def test_already_authenticated_redirects(self, db, logged_in_client):
        resp = logged_in_client.get("/login")
        assert resp.status_code == 302


class TestAuditorLogin:
    """POST /login/auditor — 監査ユーザーログイン"""

    def test_auditor_login_success(self, client, db, auditor):
        resp = client.post("/login/auditor", data={
            "username": "auditor",
            "password": "password123",
        }, follow_redirects=False)
        assert resp.status_code == 302

    def test_personal_user_cannot_use_auditor_login(self, client, db, user):
        """個人ユーザーは /login/auditor からログインできない"""
        resp = client.post("/login/auditor", data={
            "username": "testuser",
            "password": "password123",
        }, follow_redirects=False)
        assert resp.status_code == 200


class TestLogout:
    """GET /logout"""

    def test_logout_redirects_to_login(self, db, logged_in_client):
        resp = logged_in_client.get("/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_logout_unauthenticated_redirects(self, client):
        resp = client.get("/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_logout_clears_session(self, db, logged_in_client):
        """ログアウト後に保護ページへアクセスすると /login にリダイレクト"""
        logged_in_client.get("/logout")
        resp = logged_in_client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


class TestRegister:
    """POST /register"""

    def test_register_success(self, client, db, account_types):
        resp = client.post("/register", data={
            "username": "newuser",
            "email": "new@example.com",
            "password": "securepass123",
            "password_confirm": "securepass123",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert User.query.filter_by(username="newuser").first() is not None

    def test_register_duplicate_username(self, client, db, user):
        resp = client.post("/register", data={
            "username": "testuser",
            "email": "another@example.com",
            "password": "securepass123",
            "password_confirm": "securepass123",
        })
        assert resp.status_code == 200
        assert "既に使われています" in resp.data.decode()

    def test_register_duplicate_email(self, client, db, user):
        resp = client.post("/register", data={
            "username": "anotheruser",
            "email": "test@example.com",
            "password": "securepass123",
            "password_confirm": "securepass123",
        })
        assert resp.status_code == 200
        assert "既に登録されています" in resp.data.decode()

    def test_register_short_password(self, client, db):
        resp = client.post("/register", data={
            "username": "newuser2",
            "email": "new2@example.com",
            "password": "short",
            "password_confirm": "short",
        })
        assert resp.status_code == 200
        # パスワードが短い→バリデーションエラーで再表示

    def test_register_password_mismatch(self, client, db):
        resp = client.post("/register", data={
            "username": "newuser3",
            "email": "new3@example.com",
            "password": "securepass123",
            "password_confirm": "differentpass",
        })
        assert resp.status_code == 200
        assert "一致しません" in resp.data.decode()


class TestSessionProtection:
    """@login_required による未認証リダイレクト"""

    @pytest.mark.parametrize("url", [
        "/",
        "/cashbook/",
        "/journal/",
        "/csv-import/",
        "/web-import/",
        "/ofx-import/",
        "/reports/",
        "/settings/",
        "/accounts/",
        "/ai-journal/",
    ])
    def test_unauthenticated_redirect(self, client, url):
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
