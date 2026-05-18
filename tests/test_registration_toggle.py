"""新規登録の有効/無効切替 (REGISTRATION_ENABLED) のテスト。

セルフホスト運用者が自家用に限定したい場合に新規登録経路を閉じられるか、
既存ユーザーのログインは影響を受けないか、UI 上の導線も非表示になるかを検証。
"""

from app.models.user import User


class TestRegistrationEnabledDefault:
    """デフォルト (REGISTRATION_ENABLED=True) では従来通り動作する"""

    def test_get_register_page(self, client):
        resp = client.get("/register")
        assert resp.status_code == 200

    def test_get_register_auditor_page(self, client):
        resp = client.get("/register/auditor")
        assert resp.status_code == 200

    def test_login_page_shows_register_link(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "アカウント登録" in resp.get_data(as_text=True)

    def test_login_auditor_page_shows_register_link(self, client):
        resp = client.get("/login/auditor")
        assert resp.status_code == 200
        assert "監査用アカウント登録" in resp.get_data(as_text=True)

    def test_post_register_creates_user(self, client, db, account_types):
        resp = client.post(
            "/register",
            data={
                "username": "newuser",
                "email": "new@example.com",
                "password": "password123",
                "password_confirm": "password123",
            },
        )
        assert resp.status_code in (302, 303)
        assert User.query.filter_by(username="newuser").first() is not None


class TestRegistrationDisabled:
    """REGISTRATION_ENABLED=False では新規登録経路を閉じる"""

    def test_get_register_returns_404(self, client, app, monkeypatch):
        monkeypatch.setitem(app.config, "REGISTRATION_ENABLED", False)
        resp = client.get("/register")
        assert resp.status_code == 404

    def test_get_register_auditor_returns_404(self, client, app, monkeypatch):
        monkeypatch.setitem(app.config, "REGISTRATION_ENABLED", False)
        resp = client.get("/register/auditor")
        assert resp.status_code == 404

    def test_post_register_returns_404_and_creates_nothing(self, client, db, app, monkeypatch):
        monkeypatch.setitem(app.config, "REGISTRATION_ENABLED", False)
        resp = client.post(
            "/register",
            data={
                "username": "blocked",
                "email": "blocked@example.com",
                "password": "password123",
                "password_confirm": "password123",
            },
        )
        assert resp.status_code == 404
        assert User.query.filter_by(username="blocked").first() is None

    def test_post_register_auditor_returns_404_and_creates_nothing(
        self, client, db, app, monkeypatch
    ):
        monkeypatch.setitem(app.config, "REGISTRATION_ENABLED", False)
        resp = client.post(
            "/register/auditor",
            data={
                "username": "blocked-auditor",
                "email": "blocked-aud@example.com",
                "password": "password123",
                "password_confirm": "password123",
            },
        )
        assert resp.status_code == 404
        assert User.query.filter_by(username="blocked-auditor").first() is None

    def test_login_page_hides_register_link(self, client, app, monkeypatch):
        monkeypatch.setitem(app.config, "REGISTRATION_ENABLED", False)
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "アカウント登録" not in resp.get_data(as_text=True)

    def test_login_auditor_page_hides_register_link(self, client, app, monkeypatch):
        monkeypatch.setitem(app.config, "REGISTRATION_ENABLED", False)
        resp = client.get("/login/auditor")
        assert resp.status_code == 200
        assert "監査用アカウント登録" not in resp.get_data(as_text=True)

    def test_existing_user_can_still_login(self, client, user, app, monkeypatch):
        monkeypatch.setitem(app.config, "REGISTRATION_ENABLED", False)
        resp = client.post(
            "/login",
            data={"username": user.username, "password": "password123"},
        )
        assert resp.status_code in (302, 303)
