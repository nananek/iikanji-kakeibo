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
        # 通知 (auto_import / Webhook) UI は廃止
        assert "通知" not in html
        # 旧名称が使われていないこと
        assert "AI API設定" not in html
        assert "自動取込" not in html

    def test_contains_all_links(self, db, logged_in_client):
        resp = logged_in_client.get("/settings/")
        html = resp.data.decode()
        assert "/accounts/" in html
        assert "/settings/fiscal" in html
        assert "/settings/ai" in html
        # auto-import / Webhook 通知 UI は廃止
        assert "/settings/auto-import" not in html
        assert "/settings/api-keys" in html
        assert "/settings/passkeys" in html
        assert "/settings/encryption-keys" in html

    def test_personal_sees_encryption_keys_card(self, db, logged_in_client):
        resp = logged_in_client.get("/settings/")
        html = resp.data.decode()
        assert "暗号鍵管理" in html
        assert "v5.0 準備" in html

    def test_auditor_does_not_see_encryption_keys(self, app, client, auditor):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/settings/")
        html = resp.data.decode()
        # 監査ユーザーには表示しない (E2EE 機能は本人 MK 専用)
        assert "暗号鍵管理" not in html

    def test_personal_user_sees_audit(self, db, logged_in_client, user):
        assert user.user_type == "personal"
        resp = logged_in_client.get("/settings/")
        html = resp.data.decode()
        assert "監査アクセス管理" in html

    def test_auditor_does_not_see_audit(self, app, client, auditor):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/settings/")
        html = resp.data.decode()
        assert "監査アクセス管理" not in html


class TestEncryptionKeysView:
    """GET /settings/encryption-keys — E2EE 鍵管理ウィザード (v5.0 準備)"""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/settings/encryption-keys")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_personal_user_200(self, db, logged_in_client):
        resp = logged_in_client.get("/settings/encryption-keys")
        assert resp.status_code == 200
        html = resp.data.decode()
        # ウィザード骨格 + Alpine 初期化属性
        assert "encryptionKeyWizard()" in html
        assert "パスフレーズ" in html
        assert "リカバリシード" in html
        # クライアント JS が読み込まれている
        assert "js/crypto/wizard.js" in html

    def test_auditor_redirects_to_settings_index(self, app, client, auditor):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/settings/encryption-keys", follow_redirects=False)
        assert resp.status_code == 302
        assert "/settings/" in resp.headers["Location"]
        # follow_redirects=True で settings.index に到達することを確認
        # (flash は showToast で JS 経由表示のため、Unicode-escape された
        #  形で HTML に埋め込まれる → 内容文字列の直接 assert は脆い)
        resp2 = client.get("/settings/encryption-keys", follow_redirects=True)
        assert resp2.status_code == 200
        # ウィザード骨格は表示されない (= リダイレクト成功)
        assert "encryptionKeyWizard()" not in resp2.data.decode()

    def test_warning_banner_present(self, db, logged_in_client):
        """v5.0 準備中である旨の警告バナーが表示されること。"""
        resp = logged_in_client.get("/settings/encryption-keys")
        html = resp.data.decode()
        assert "プレビュー機能" in html or "v5.0" in html


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
