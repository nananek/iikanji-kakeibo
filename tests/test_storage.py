"""ストレージサービスのユニットテスト"""

import importlib
import os

import pytest

from app.services.storage import (
    LocalStorageBackend,
    make_storage_key,
    get_storage_backend,
    reset_storage_backend,
)


# --- make_storage_key ---


class TestMakeStorageKey:
    def test_jpeg(self):
        assert make_storage_key(1, 42, "image/jpeg") == "vouchers/1/42.jpg"

    def test_png(self):
        assert make_storage_key(1, 42, "image/png") == "vouchers/1/42.png"

    def test_webp(self):
        assert make_storage_key(1, 42, "image/webp") == "vouchers/1/42.webp"

    def test_gif(self):
        assert make_storage_key(1, 42, "image/gif") == "vouchers/1/42.gif"

    def test_unknown_mime(self):
        assert make_storage_key(1, 42, "application/pdf") == "vouchers/1/42.bin"


# --- LocalStorageBackend ---


class TestLocalStorageBackend:
    @pytest.fixture
    def backend(self, tmp_path):
        return LocalStorageBackend(str(tmp_path))

    def test_put_and_get(self, backend):
        backend.put("test/file.jpg", b"image-data", "image/jpeg")
        assert backend.get("test/file.jpg") == b"image-data"

    def test_get_not_found(self, backend):
        with pytest.raises(FileNotFoundError):
            backend.get("nonexistent.jpg")

    def test_delete(self, backend):
        backend.put("test/file.jpg", b"data", "image/jpeg")
        backend.delete("test/file.jpg")
        assert not backend.exists("test/file.jpg")

    def test_delete_nonexistent(self, backend):
        backend.delete("nonexistent.jpg")

    def test_exists_false(self, backend):
        assert not backend.exists("test/file.jpg")

    def test_exists_true(self, backend):
        backend.put("test/file.jpg", b"data", "image/jpeg")
        assert backend.exists("test/file.jpg")

    def test_path_traversal_prevention(self, backend):
        with pytest.raises(ValueError, match="Invalid storage key"):
            backend.put("../../etc/passwd", b"data", "text/plain")

    def test_creates_parent_directories(self, backend):
        backend.put("a/b/c/file.jpg", b"data", "image/jpeg")
        assert backend.get("a/b/c/file.jpg") == b"data"

    def test_overwrite(self, backend):
        backend.put("test/file.jpg", b"old", "image/jpeg")
        backend.put("test/file.jpg", b"new", "image/jpeg")
        assert backend.get("test/file.jpg") == b"new"


# --- get_storage_backend ファクトリ ---


class TestGetStorageBackend:
    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_storage_backend()
        yield
        reset_storage_backend()

    def test_returns_local_backend(self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "STORAGE_BACKEND", "local")
        monkeypatch.setitem(app.config, "STORAGE_LOCAL_DIR", str(tmp_path))
        with app.app_context():
            backend = get_storage_backend()
            assert isinstance(backend, LocalStorageBackend)

    def test_singleton(self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "STORAGE_BACKEND", "local")
        monkeypatch.setitem(app.config, "STORAGE_LOCAL_DIR", str(tmp_path))
        with app.app_context():
            b1 = get_storage_backend()
            b2 = get_storage_backend()
            assert b1 is b2


# --- マイグレーション安全チェック ---


def _load_migration():
    """数字始まりのモジュール名を動的インポート"""
    return importlib.import_module(
        "migrations.versions.023_externalize_image_storage"
    )


class TestVolumeMountCheck:
    def test_non_docker_passes(self, monkeypatch):
        """非Docker環境では RuntimeError を投げない"""
        m = _load_migration()
        monkeypatch.setattr(m, "_is_docker_environment", lambda: False)
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        m._check_storage_safety("/tmp/test")

    def test_s3_skips_check(self, monkeypatch):
        """S3バックエンド時はチェックをスキップ"""
        m = _load_migration()
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        m._check_storage_safety("/app/data/vouchers")

    def test_docker_without_mount_raises(self, monkeypatch):
        """Docker環境でマウントなしの場合に RuntimeError"""
        m = _load_migration()
        monkeypatch.setattr(m, "_is_docker_environment", lambda: True)
        monkeypatch.setattr(m, "_is_volume_mounted", lambda p: False)
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        with pytest.raises(RuntimeError, match="ボリュームマウントされていません"):
            m._check_storage_safety("/app/data/vouchers")

    def test_docker_with_mount_passes(self, monkeypatch):
        """Docker環境でマウント済みの場合は通過"""
        m = _load_migration()
        monkeypatch.setattr(m, "_is_docker_environment", lambda: True)
        monkeypatch.setattr(m, "_is_volume_mounted", lambda p: True)
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        m._check_storage_safety("/app/data/vouchers")
