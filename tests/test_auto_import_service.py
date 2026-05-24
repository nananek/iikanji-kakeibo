"""自動取込サービス (services/auto_import.py) のテスト"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.auto_import import AutoImportSource, ProcessedFile, WebhookConfig
from app.services.auto_import import (
    _build_provider,
    _process_source,
    decrypt_credentials,
    encrypt_credentials,
    run_auto_import,
)
from app.services.sources import SourceFile


def _ai_config(db, user_id):
    from app.services.ai_receipt import encrypt_api_key
    cfg = UserAIConfig(
        user_id=user_id, provider="openai",
        api_key_encrypted=encrypt_api_key("k"), model_name="gpt-4",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _source(db, user_id, name="Src"):
    s = AutoImportSource(
        user_id=user_id, name=name, provider="webdav",
        config_json=json.dumps({
            "url": "https://example.com/dav",
            "username": "u", "folder_path": "/",
            "file_extensions": ["jpg", "png"],
        }),
        credentials_encrypted=encrypt_credentials({"password": "p"}),
        is_active=True,
    )
    db.session.add(s)
    db.session.commit()
    return s


class TestEncryptDecrypt:
    def test_roundtrip(self, app):
        with app.app_context():
            data = {"password": "secret", "token": "x"}
            enc = encrypt_credentials(data)
            assert isinstance(enc, bytes)
            dec = decrypt_credentials(enc)
            assert dec == data

    def test_decrypt_invalid(self, app):
        with app.app_context():
            with pytest.raises(ValueError):
                decrypt_credentials(b"not-encrypted")


class TestBuildProvider:
    def test_webdav(self, db, user, accounts):
        s = _source(db, user.id)
        provider = _build_provider(s)
        from app.services.sources.webdav import WebDAVProvider
        assert isinstance(provider, WebDAVProvider)

    def test_unknown_provider(self, db, user, accounts):
        s = AutoImportSource(
            user_id=user.id, name="X", provider="unknown",
            config_json=json.dumps({"url": "x"}),
            credentials_encrypted=encrypt_credentials({"password": "p"}),
        )
        db.session.add(s)
        db.session.commit()
        with pytest.raises(ValueError):
            _build_provider(s)


class TestRunAutoImport:
    def test_no_ai_config(self, db, user, accounts):
        stats = run_auto_import(user.id, dry_run=True)
        assert stats["sources_processed"] == 0
        assert any("AI API設定" in e for e in stats["errors"])

    def test_no_sources(self, db, user, accounts):
        _ai_config(db, user.id)
        stats = run_auto_import(user.id, dry_run=True)
        assert stats["sources_processed"] == 0
        assert stats["errors"] == []

    def test_full_dry_run(self, db, user, accounts):
        _ai_config(db, user.id)
        s = _source(db, user.id)
        with patch("app.services.auto_import._build_provider") as mock_b:
            provider = MagicMock()
            provider.list_files.return_value = [
                SourceFile(path="/a.jpg", etag="e1", size=1024,
                           mime_type="image/jpeg"),
            ]
            provider.download_file.return_value = b"image-bytes"
            mock_b.return_value = provider

            stats = run_auto_import(user.id, dry_run=True)
        assert stats["sources_processed"] == 1
        assert stats["files_found"] == 1
        assert stats["files_new"] == 1
        # dry run なので drafts_created もカウントされるが DB には書かない
        assert AIDraft.query.filter_by(user_id=user.id).count() == 0

    def test_real_run_creates_pending_draft(self, db, user, accounts):
        """E2 PR-C-4g: サーバ側 AI 解析を行わず、pending ドラフトを作成。"""
        _ai_config(db, user.id)
        s = _source(db, user.id)
        with patch("app.services.auto_import._build_provider") as mock_b, \
             patch("app.services.auto_import.store_image_with_thumbnail"):
            provider = MagicMock()
            provider.list_files.return_value = [
                SourceFile(path="/a.jpg", etag="e1", size=1024,
                           mime_type="image/jpeg"),
            ]
            provider.download_file.return_value = b"image-bytes"
            mock_b.return_value = provider

            stats = run_auto_import(user.id, dry_run=False)
        assert stats["drafts_created"] == 1
        drafts = AIDraft.query.filter_by(user_id=user.id).all()
        assert len(drafts) == 1
        # E2 PR-C-4g: status='pending' で suggestions は空
        assert drafts[0].status == "pending"
        assert drafts[0].suggestions_json == "[]"
        assert ProcessedFile.query.filter_by(source_id=s.id).count() == 1

    def test_skip_already_processed(self, db, user, accounts):
        _ai_config(db, user.id)
        s = _source(db, user.id)
        # 既に処理済み
        db.session.add(ProcessedFile(
            source_id=s.id, file_path="/a.jpg",
            etag="e1", status="success",
        ))
        db.session.commit()

        with patch("app.services.auto_import._build_provider") as mock_b:
            provider = MagicMock()
            provider.list_files.return_value = [
                SourceFile(path="/a.jpg", etag="e1", size=1024,
                           mime_type="image/jpeg"),
            ]
            mock_b.return_value = provider

            stats = run_auto_import(user.id, dry_run=True)
        assert stats["files_found"] == 1
        assert stats["files_new"] == 0
        # download も走らない (skip 判定で早期 continue)
        provider.download_file.assert_not_called()

    def test_reprocess_when_etag_changed(self, db, user, accounts):
        _ai_config(db, user.id)
        s = _source(db, user.id)
        db.session.add(ProcessedFile(
            source_id=s.id, file_path="/a.jpg",
            etag="OLD", status="success",
        ))
        db.session.commit()

        with patch("app.services.auto_import._build_provider") as mock_b:
            provider = MagicMock()
            provider.list_files.return_value = [
                SourceFile(path="/a.jpg", etag="NEW", size=1024,
                           mime_type="image/jpeg"),
            ]
            provider.download_file.return_value = b"x"
            mock_b.return_value = provider

            stats = run_auto_import(user.id, dry_run=True)
        assert stats["files_new"] == 1

    def test_skip_oversized(self, db, user, accounts):
        _ai_config(db, user.id)
        s = _source(db, user.id)
        with patch("app.services.auto_import._build_provider") as mock_b:
            provider = MagicMock()
            provider.list_files.return_value = [
                SourceFile(path="/big.jpg", etag="e1",
                           size=11 * 1024 * 1024,  # 11MB > MAX_IMAGE_SIZE
                           mime_type="image/jpeg"),
            ]
            mock_b.return_value = provider

            stats = run_auto_import(user.id, dry_run=True)
        assert stats["files_new"] == 1  # カウントされるが処理スキップ
        assert stats["drafts_created"] == 0

    def test_skip_unsupported_mime(self, db, user, accounts):
        _ai_config(db, user.id)
        s = _source(db, user.id)
        with patch("app.services.auto_import._build_provider") as mock_b:
            provider = MagicMock()
            provider.list_files.return_value = [
                SourceFile(path="/x.txt", etag="e1", size=100,
                           mime_type="text/plain"),
            ]
            mock_b.return_value = provider

            stats = run_auto_import(user.id, dry_run=True)
        assert stats["drafts_created"] == 0

    def test_provider_error_recorded(self, db, user, accounts):
        _ai_config(db, user.id)
        s = _source(db, user.id, name="ErrSrc")
        with patch("app.services.auto_import._build_provider") as mock_b:
            mock_b.side_effect = ValueError("config error")
            stats = run_auto_import(user.id, dry_run=True)
        assert stats["sources_processed"] == 1
        assert any("ErrSrc" in e for e in stats["errors"])

    def test_too_many_files_truncated(self, db, user, accounts):
        _ai_config(db, user.id)
        s = _source(db, user.id)
        with patch("app.services.auto_import._build_provider") as mock_b:
            provider = MagicMock()
            # 150 files
            provider.list_files.return_value = [
                SourceFile(path=f"/a{i}.jpg", etag=f"e{i}", size=100,
                           mime_type="image/jpeg")
                for i in range(150)
            ]
            provider.download_file.return_value = b"x"
            mock_b.return_value = provider

            stats = run_auto_import(user.id, dry_run=True)
        # MAX_FILES_PER_SOURCE = 100 で打ち切られる
        assert stats["files_found"] == 100

    def test_download_failure_records_error(self, db, user, accounts):
        """E2 PR-C-4g: AI 解析を行わないため失敗源は download/storage のみ。
        旧 test_analyze_failure_records_error の置換。"""
        _ai_config(db, user.id)
        s = _source(db, user.id)
        with patch("app.services.auto_import._build_provider") as mock_b:
            provider = MagicMock()
            provider.list_files.return_value = [
                SourceFile(path="/a.jpg", etag="e1", size=100,
                           mime_type="image/jpeg"),
            ]
            provider.download_file.side_effect = RuntimeError("WebDAV down")
            mock_b.return_value = provider

            stats = run_auto_import(user.id, dry_run=False)
        assert any("WebDAV down" in e for e in stats["errors"])
        pf = ProcessedFile.query.filter_by(source_id=s.id).first()
        assert pf is not None
        assert pf.status == "error"


class TestNotifyUser:
    def test_notify_on_success(self, db, user, accounts):
        _ai_config(db, user.id)
        s = _source(db, user.id)
        wh = WebhookConfig(
            user_id=user.id, name="hook", provider="discord",
            webhook_url="https://discord.com/api/webhooks/x/y",
            events_json='["import_success"]',
            is_active=True,
        )
        db.session.add(wh)
        db.session.commit()

        with patch("app.services.auto_import._build_provider") as mock_b, \
             patch("app.services.auto_import.store_image_with_thumbnail"), \
             patch("app.services.auto_import.send_webhook") as mock_send:
            provider = MagicMock()
            provider.list_files.return_value = [
                SourceFile(path="/a.jpg", etag="e1", size=100,
                           mime_type="image/jpeg"),
            ]
            provider.download_file.return_value = b"x"
            mock_b.return_value = provider

            run_auto_import(user.id, dry_run=False)
            mock_send.assert_called()

    def test_notify_on_error(self, db, user, accounts):
        _ai_config(db, user.id)
        s = _source(db, user.id)
        wh = WebhookConfig(
            user_id=user.id, name="hook", provider="discord",
            webhook_url="https://discord.com/api/webhooks/x/y",
            events_json='["import_error"]',
            is_active=True,
        )
        db.session.add(wh)
        db.session.commit()

        with patch("app.services.auto_import._build_provider") as mock_b, \
             patch("app.services.auto_import.send_webhook") as mock_send:
            mock_b.side_effect = ValueError("boom")
            run_auto_import(user.id, dry_run=False)
            mock_send.assert_called()

    def test_skip_notify_when_no_event_match(self, db, user, accounts):
        _ai_config(db, user.id)
        s = _source(db, user.id)
        # import_success のみだが drafts_created=0 なので発火しない
        wh = WebhookConfig(
            user_id=user.id, name="hook", provider="discord",
            webhook_url="https://discord.com/api/webhooks/x/y",
            events_json='["import_success"]',
            is_active=True,
        )
        db.session.add(wh)
        db.session.commit()

        with patch("app.services.auto_import._build_provider") as mock_b, \
             patch("app.services.auto_import.send_webhook") as mock_send:
            provider = MagicMock()
            provider.list_files.return_value = []
            mock_b.return_value = provider

            run_auto_import(user.id, dry_run=False)
            mock_send.assert_not_called()
