"""設定ビューのテスト"""

import pytest


class TestSettingsIndex:
    """GET /settings/ — 設定トップページ"""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/settings/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_authenticated_returns_200(self, db, logged_in_client):
        resp = logged_in_client.get("/settings/")
        assert resp.status_code == 200

    def test_contains_category_headings(self, db, logged_in_client):
        resp = logged_in_client.get("/settings/")
        html = resp.data.decode()
        assert "帳簿" in html
        assert "AI・連携" in html
        assert "セキュリティ" in html

    def test_contains_renamed_labels(self, db, logged_in_client):
        resp = logged_in_client.get("/settings/")
        html = resp.data.decode()
        assert "外部AI" in html
        assert "通知" in html
        # 旧名称が使われていないこと
        assert "AI API設定" not in html
        assert "自動取込" not in html

    def test_contains_all_links(self, db, logged_in_client):
        resp = logged_in_client.get("/settings/")
        html = resp.data.decode()
        assert "/accounts/" in html
        assert "/settings/fiscal" in html
        assert "/settings/ai" in html
        assert "/settings/auto-import" in html
        assert "/settings/api-keys" in html
        assert "/settings/passkeys" in html

    def test_personal_user_sees_audit(self, db, logged_in_client, user):
        assert user.user_type == "personal"
        resp = logged_in_client.get("/settings/")
        html = resp.data.decode()
        assert "顧問アクセス管理" in html

    def test_auditor_does_not_see_audit(self, app, client, auditor):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/settings/")
        html = resp.data.decode()
        assert "顧問アクセス管理" not in html


class TestDisplaySettings:
    """GET/POST /settings/display — 表示設定（予測方法）"""

    def test_display_shows_projection_method(self, db, logged_in_client):
        resp = logged_in_client.get("/settings/display")
        html = resp.data.decode()
        assert "projection_method" in html
        assert "日割按分" in html
        assert "28日移動平均" in html
        assert "曜日別平均" in html

    def test_save_projection_method(self, db, logged_in_client, user):
        resp = logged_in_client.post("/settings/display/save", data={
            "default_period": "all",
            "ledger_sort": "asc",
            "projection_method": "rolling28",
        }, follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(user)
        assert user.get_pref("projection_method") == "rolling28"

    def test_invalid_projection_method_defaults(self, db, logged_in_client, user):
        resp = logged_in_client.post("/settings/display/save", data={
            "default_period": "all",
            "ledger_sort": "asc",
            "projection_method": "invalid",
        }, follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(user)
        assert user.get_pref("projection_method") == "pro_rata"
