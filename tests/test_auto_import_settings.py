"""自動取込設定 (settings.py の auto_import 系) のテスト"""

import json
from unittest.mock import patch

from app.models.auto_import import AutoImportSource, WebhookConfig


class TestSourceAdd:
    def test_unauthenticated(self, client):
        resp = client.get("/settings/auto-import/sources/add")
        assert resp.status_code in (302, 401)

    def test_get_renders_form(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/auto-import/sources/add")
        assert resp.status_code == 200

    def test_missing_required(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/auto-import/sources/add", data={
            "name": "", "url": "", "username": "", "password": "",
        })
        assert resp.status_code == 200

    def test_too_long_name(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/auto-import/sources/add", data={
            "name": "x" * 101,
            "url": "https://example.com/dav",
            "username": "u", "password": "p",
        })
        assert resp.status_code == 200

    def test_invalid_url(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/auto-import/sources/add", data={
            "name": "test",
            "url": "ftp://example.com/dav",  # http/https 以外
            "username": "u", "password": "p",
        })
        assert resp.status_code == 200

    def test_private_ip_blocked(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/auto-import/sources/add", data={
            "name": "test",
            "url": "http://127.0.0.1/dav",  # ループバック
            "username": "u", "password": "p",
        })
        assert resp.status_code == 200

    def test_connection_test_failure(self, db, logged_in_client, user, accounts):
        with patch("app.services.sources.validate_external_url") as mock_v, \
             patch("app.services.sources.webdav.WebDAVProvider.test_connection") as mock_t:
            mock_v.return_value = (True, None)
            mock_t.return_value = (False, "HTTP 401")
            resp = logged_in_client.post("/settings/auto-import/sources/add", data={
                "name": "test",
                "url": "https://example.com/dav",
                "username": "u", "password": "p",
            })
            assert resp.status_code == 200
        assert AutoImportSource.query.filter_by(user_id=user.id).count() == 0

    def test_success(self, db, logged_in_client, user, accounts):
        with patch("app.services.sources.validate_external_url") as mock_v, \
             patch("app.services.sources.webdav.WebDAVProvider.test_connection") as mock_t:
            mock_v.return_value = (True, None)
            mock_t.return_value = (True, None)
            resp = logged_in_client.post("/settings/auto-import/sources/add", data={
                "name": "MyDav",
                "url": "https://example.com/dav",
                "username": "u", "password": "p",
                "folder_path": "/photos",
            })
        assert resp.status_code in (302, 303)
        s = AutoImportSource.query.filter_by(user_id=user.id, name="MyDav").first()
        assert s is not None


class TestSourceTest:
    def test_unauthenticated(self, client):
        resp = client.post("/settings/auto-import/sources/test")
        assert resp.status_code in (302, 401)

    def test_missing_required(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/auto-import/sources/test", data={
            "url": "", "username": "", "password": "",
        })
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False

    def test_invalid_url(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/auto-import/sources/test", data={
            "url": "ftp://example.com/", "username": "u", "password": "p",
        })
        assert resp.status_code == 400

    def test_success(self, logged_in_client, accounts):
        with patch("app.services.sources.validate_external_url") as mock_v, \
             patch("app.services.sources.webdav.WebDAVProvider.test_connection") as mock_t, \
             patch("app.services.sources.webdav.WebDAVProvider.list_files") as mock_l:
            mock_v.return_value = (True, None)
            mock_t.return_value = (True, None)
            mock_l.return_value = []
            resp = logged_in_client.post("/settings/auto-import/sources/test", data={
                "url": "https://example.com/dav",
                "username": "u", "password": "p",
            })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True

    def test_connection_failure(self, logged_in_client, accounts):
        with patch("app.services.sources.validate_external_url") as mock_v, \
             patch("app.services.sources.webdav.WebDAVProvider.test_connection") as mock_t:
            mock_v.return_value = (True, None)
            mock_t.return_value = (False, "HTTP 401")
            resp = logged_in_client.post("/settings/auto-import/sources/test", data={
                "url": "https://example.com/dav",
                "username": "u", "password": "p",
            })
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False


class TestSourceTestExisting:
    def _make_source(self, db, user_id):
        from app.services.auto_import import encrypt_credentials
        s = AutoImportSource(
            user_id=user_id, name="Test",
            provider="webdav",
            config_json=json.dumps({
                "url": "https://example.com/dav",
                "username": "u", "folder_path": "/",
                "file_extensions": ["jpg"],
            }),
            credentials_encrypted=encrypt_credentials({"password": "p"}),
        )
        db.session.add(s)
        db.session.commit()
        return s

    def test_404(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/settings/auto-import/sources/9999/test"
        )
        assert resp.status_code == 404

    def test_idor(self, db, logged_in_client, accounts, second_user):
        s = self._make_source(db, second_user.id)
        resp = logged_in_client.post(
            f"/settings/auto-import/sources/{s.id}/test"
        )
        assert resp.status_code == 404

    def test_test_connection_success(self, db, logged_in_client, user, accounts):
        s = self._make_source(db, user.id)
        with patch("app.services.auto_import._build_provider") as mock_b:
            from unittest.mock import MagicMock
            provider = MagicMock()
            provider.test_connection.return_value = (True, None)
            provider.list_files.return_value = []
            mock_b.return_value = provider
            resp = logged_in_client.post(
                f"/settings/auto-import/sources/{s.id}/test"
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True

    def test_build_provider_error(self, db, logged_in_client, user, accounts):
        s = self._make_source(db, user.id)
        with patch("app.services.auto_import._build_provider") as mock_b:
            mock_b.side_effect = ValueError("BAD config")
            resp = logged_in_client.post(
                f"/settings/auto-import/sources/{s.id}/test"
            )
        assert resp.status_code == 400


class TestSourceToggle:
    def _make_source(self, db, user_id, is_active=True):
        from app.services.auto_import import encrypt_credentials
        s = AutoImportSource(
            user_id=user_id, name="X", provider="webdav",
            config_json=json.dumps({"url": "u"}),
            credentials_encrypted=encrypt_credentials({"password": "p"}),
            is_active=is_active,
        )
        db.session.add(s)
        db.session.commit()
        return s

    def test_404(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/settings/auto-import/sources/9999/toggle"
        )
        assert resp.status_code == 404

    def test_idor(self, db, logged_in_client, accounts, second_user):
        s = self._make_source(db, second_user.id)
        resp = logged_in_client.post(
            f"/settings/auto-import/sources/{s.id}/toggle"
        )
        assert resp.status_code == 404

    def test_toggle_disables(self, db, logged_in_client, user, accounts):
        s = self._make_source(db, user.id, is_active=True)
        resp = logged_in_client.post(
            f"/settings/auto-import/sources/{s.id}/toggle"
        )
        assert resp.status_code in (302, 303)
        db.session.refresh(s)
        assert s.is_active is False

    def test_toggle_enables(self, db, logged_in_client, user, accounts):
        s = self._make_source(db, user.id, is_active=False)
        resp = logged_in_client.post(
            f"/settings/auto-import/sources/{s.id}/toggle"
        )
        assert resp.status_code in (302, 303)
        db.session.refresh(s)
        assert s.is_active is True


class TestSourceDelete:
    def _make_source(self, db, user_id):
        from app.services.auto_import import encrypt_credentials
        s = AutoImportSource(
            user_id=user_id, name="X", provider="webdav",
            config_json=json.dumps({"url": "u"}),
            credentials_encrypted=encrypt_credentials({"password": "p"}),
        )
        db.session.add(s)
        db.session.commit()
        return s

    def test_404(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/settings/auto-import/sources/9999/delete"
        )
        assert resp.status_code == 404

    def test_idor(self, db, logged_in_client, accounts, second_user):
        s = self._make_source(db, second_user.id)
        resp = logged_in_client.post(
            f"/settings/auto-import/sources/{s.id}/delete"
        )
        assert resp.status_code == 404

    def test_delete(self, db, logged_in_client, user, accounts):
        s = self._make_source(db, user.id)
        sid = s.id
        resp = logged_in_client.post(
            f"/settings/auto-import/sources/{sid}/delete"
        )
        assert resp.status_code in (302, 303)
        assert db.session.get(AutoImportSource, sid) is None


class TestWebhookAdd:
    def test_unauthenticated(self, client):
        resp = client.get("/settings/auto-import/webhooks/add")
        assert resp.status_code in (302, 401)

    def test_get_renders(self, logged_in_client, accounts):
        resp = logged_in_client.get("/settings/auto-import/webhooks/add")
        assert resp.status_code == 200

    def test_missing_required(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/auto-import/webhooks/add", data={
            "name": "", "webhook_url": "",
        })
        assert resp.status_code == 200

    def test_too_long_name(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/auto-import/webhooks/add", data={
            "name": "x" * 101,
            "webhook_url": "https://discord.com/api/webhooks/x",
        })
        assert resp.status_code == 200

    def test_wrong_prefix(self, logged_in_client, accounts):
        resp = logged_in_client.post("/settings/auto-import/webhooks/add", data={
            "name": "test",
            "webhook_url": "https://example.com/wh",  # discord.com 以外
            "provider": "discord",
        })
        assert resp.status_code == 200

    def test_invalid_url(self, logged_in_client, accounts):
        with patch("app.services.sources.validate_external_url") as mock_v:
            mock_v.return_value = (False, "private")
            resp = logged_in_client.post("/settings/auto-import/webhooks/add", data={
                "name": "test",
                "webhook_url": "https://discord.com/api/webhooks/x",
                "provider": "discord",
            })
        assert resp.status_code == 200

    def test_success(self, db, logged_in_client, user, accounts):
        with patch("app.services.sources.validate_external_url") as mock_v:
            mock_v.return_value = (True, None)
            resp = logged_in_client.post("/settings/auto-import/webhooks/add", data={
                "name": "MyHook",
                "webhook_url": "https://discord.com/api/webhooks/x/y",
                "provider": "discord",
                "events": ["import_success"],
            })
        assert resp.status_code in (302, 303)
        wh = WebhookConfig.query.filter_by(user_id=user.id, name="MyHook").first()
        assert wh is not None

    def test_success_default_events(self, db, logged_in_client, user, accounts):
        """events 指定なしの場合は import_success がデフォルト"""
        with patch("app.services.sources.validate_external_url") as mock_v:
            mock_v.return_value = (True, None)
            resp = logged_in_client.post("/settings/auto-import/webhooks/add", data={
                "name": "Default",
                "webhook_url": "https://discord.com/api/webhooks/x/y",
                "provider": "discord",
            })
        assert resp.status_code in (302, 303)
        wh = WebhookConfig.query.filter_by(user_id=user.id, name="Default").first()
        assert "import_success" in wh.events_json


class TestWebhookDelete:
    def _make_webhook(self, db, user_id):
        wh = WebhookConfig(
            user_id=user_id, name="WH", provider="discord",
            webhook_url="https://discord.com/api/webhooks/x/y",
            events_json='["import_success"]',
        )
        db.session.add(wh)
        db.session.commit()
        return wh

    def test_404(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/settings/auto-import/webhooks/9999/delete"
        )
        assert resp.status_code == 404

    def test_idor(self, db, logged_in_client, accounts, second_user):
        wh = self._make_webhook(db, second_user.id)
        resp = logged_in_client.post(
            f"/settings/auto-import/webhooks/{wh.id}/delete"
        )
        assert resp.status_code == 404

    def test_delete(self, db, logged_in_client, user, accounts):
        wh = self._make_webhook(db, user.id)
        whid = wh.id
        resp = logged_in_client.post(
            f"/settings/auto-import/webhooks/{whid}/delete"
        )
        assert resp.status_code in (302, 303)
        assert db.session.get(WebhookConfig, whid) is None
