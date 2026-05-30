"""入力検証セキュリティテスト — パストラバーサル、オープンリダイレクト、XSS"""

import re

import pytest

from app.models.user import User
from tests.conftest import make_journal


# ---------------------------------------------------------------------------
# パストラバーサル (helpers.py の一時ファイル操作)
# ---------------------------------------------------------------------------

class TestPathTraversal:
    """load_import_data / delete_import_data が _TEMP_DIR の外を参照しないことを検証"""

    def test_load_traversal_returns_none(self, app):
        from app.views.helpers import load_import_data
        with app.app_context():
            assert load_import_data("../../etc/passwd") is None

    def test_load_absolute_path_returns_none(self, app):
        from app.views.helpers import load_import_data
        with app.app_context():
            assert load_import_data("/etc/passwd") is None

    def test_delete_traversal_is_noop(self, app):
        from app.views.helpers import delete_import_data
        with app.app_context():
            # 例外が出ないこと、外部ファイルが消えないこと
            delete_import_data("../../etc/important_file")

    def test_roundtrip_with_valid_key(self, app):
        from app.views.helpers import save_import_data, load_import_data, delete_import_data
        with app.app_context():
            key = save_import_data({"test": True})
            assert re.match(r"^[0-9a-f\-]+$", key)
            data = load_import_data(key)
            assert data == {"test": True}
            delete_import_data(key)
            assert load_import_data(key) is None

    def test_load_empty_key_returns_none(self, app):
        from app.views.helpers import load_import_data
        with app.app_context():
            assert load_import_data("") is None
            assert load_import_data(None) is None


# ---------------------------------------------------------------------------
# オープンリダイレクト (_safe_next_url)
# ---------------------------------------------------------------------------

class TestOpenRedirect:
    """ログイン後の next パラメータが外部URLに誘導されないことを検証"""

    def test_safe_internal_redirect(self, client, db, user):
        resp = client.post("/login?next=/cashbook/", data={
            "username": "testuser",
            "password": "password123",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert "/cashbook/" in resp.headers["Location"]

    def test_blocks_external_redirect(self, client, db, user):
        resp = client.post("/login?next=https://evil.com/", data={
            "username": "testuser",
            "password": "password123",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert "evil.com" not in resp.headers["Location"]

    def test_blocks_protocol_relative_redirect(self, client, db, user):
        resp = client.post("/login?next=//evil.com/", data={
            "username": "testuser",
            "password": "password123",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert "evil.com" not in resp.headers["Location"]

    def test_blocks_javascript_scheme(self, client, db, user):
        resp = client.post("/login?next=javascript:alert(1)", data={
            "username": "testuser",
            "password": "password123",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert "javascript" not in resp.headers["Location"]


# ---------------------------------------------------------------------------
# XSS エスケープ
# ---------------------------------------------------------------------------

class TestXSSEscaping:
    """テンプレートが自動エスケープされることを検証"""

    def test_script_tag_in_journal_description_not_server_rendered(self, app, db,
                                                                   logged_in_client, user,
                                                                   accounts):
        """仕訳一覧 (E3-F PR-D-4-3 でクライアント描画) は平文 description を
        サーバ出力しない。XSS ペイロードがサーバ HTML に一切現れないことを担保
        (クライアント側 index_renderer.mjs は textContent で描画し DOM XSS も防ぐ)。"""
        make_journal(db, user.id, "5010", "1010",
                     1000, description='<script>alert("xss")</script>')
        resp = logged_in_client.get("/journal/?year=2026")
        html = resp.data.decode()
        # 生の <script> タグもエスケープ済み文字列も出力されない (description 非読取)
        assert '<script>alert("xss")</script>' not in html
        assert 'alert(&#34;xss&#34;)' not in html
        assert 'alert("xss")' not in html

    def test_json_endpoint_returns_json_content_type(self, app, db,
                                                      logged_in_client, user,
                                                      accounts):
        """JSON エンドポイントは application/json で返す"""
        entry = make_journal(db, user.id, "5010", "1010",
                             1000, description='<img src=x onerror=alert(1)>')
        resp = logged_in_client.get(f"/journal/{entry.id}/json")
        assert resp.content_type.startswith("application/json")

    def test_img_onerror_in_description_escaped(self, app, db,
                                                  logged_in_client, user,
                                                  accounts):
        """img onerror XSS ペイロードがエスケープされる"""
        make_journal(db, user.id, "5010", "1010",
                     1000, description='<img src=x onerror=alert(1)>')
        resp = logged_in_client.get("/journal/")
        html = resp.data.decode()
        assert '<img src=x onerror=alert(1)>' not in html
