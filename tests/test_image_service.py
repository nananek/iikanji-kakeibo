"""画像配信サービス (services/image.py) のテスト

ローカル / S3 / 汎用バックエンドそれぞれのパスと、ETag 条件付きリクエスト・
サムネイルフォールバックを網羅。
"""

from unittest.mock import MagicMock, patch

import pytest


class TestServeImage:
    def test_etag_match_returns_304(self, app):
        with app.test_request_context(headers={"If-None-Match": '"abc123"'}):
            from app.services.image import serve_image
            with patch("app.services.image.get_storage_backend") as mock_b:
                mock_b.return_value = MagicMock()
                resp = serve_image("vouchers/1/x.jpg", "image/jpeg",
                                    file_hash="abc123")
                assert resp.status_code == 304

    def test_etag_mismatch_serves_image(self, app, tmp_path):
        # local backend のテスト
        img = tmp_path / "x.jpg"
        img.write_bytes(b"image-bytes")

        with app.test_request_context(headers={"If-None-Match": '"old"'}):
            from app.services.image import serve_image
            with patch("app.services.image.get_storage_backend") as mock_b, \
                 patch("app.services.image.isinstance") as mock_isinstance:
                from app.services.storage import LocalStorageBackend
                backend = MagicMock(spec=LocalStorageBackend)
                backend.full_path.return_value = img
                backend.exists.return_value = True
                mock_b.return_value = backend
                # isinstance(backend, LocalStorageBackend) を True に
                mock_isinstance.side_effect = lambda obj, cls: cls.__name__ == "LocalStorageBackend"
                resp = serve_image("vouchers/1/x.jpg", "image/jpeg",
                                    file_hash="new")
                assert resp.status_code == 200

    def test_thumb_when_exists(self, app):
        with app.test_request_context(query_string={"size": "thumb"}):
            from app.services.image import serve_image
            with patch("app.services.image.get_storage_backend") as mock_b:
                backend = MagicMock()
                backend.exists.return_value = True
                backend.get.return_value = b"thumb-bytes"
                mock_b.return_value = backend
                resp = serve_image("vouchers/1/x.jpg", "image/jpeg")
                # 汎用パス
                assert resp.status_code in (200, 302)

    def test_thumb_not_exists_falls_back(self, app):
        with app.test_request_context(query_string={"size": "thumb"}):
            from app.services.image import serve_image
            with patch("app.services.image.get_storage_backend") as mock_b:
                backend = MagicMock()
                backend.exists.return_value = False
                backend.get.return_value = b"original-bytes"
                mock_b.return_value = backend
                resp = serve_image("vouchers/1/x.jpg", "image/jpeg")
                assert resp.status_code == 200

    def test_local_serve_file_not_found(self, app, tmp_path):
        with app.test_request_context():
            from app.services.image import serve_image
            from app.services.storage import LocalStorageBackend
            with patch("app.services.image.get_storage_backend") as mock_b:
                backend = MagicMock(spec=LocalStorageBackend)
                missing = tmp_path / "missing.jpg"
                backend.full_path.return_value = missing
                backend.exists.return_value = False
                mock_b.return_value = backend
                with pytest.raises(FileNotFoundError):
                    serve_image("missing.jpg", "image/jpeg")

    def test_s3_redirects_to_presigned(self, app):
        with app.test_request_context():
            from app.services.image import serve_image
            from app.services.storage import S3StorageBackend
            with patch("app.services.image.get_storage_backend") as mock_b:
                backend = MagicMock(spec=S3StorageBackend)
                backend.exists.return_value = False  # for thumb check
                backend.generate_presigned_url.return_value = "https://s3.example/x?sig=y"
                mock_b.return_value = backend
                resp = serve_image("vouchers/1/x.jpg", "image/jpeg")
                assert resp.status_code == 302
                assert "s3.example" in resp.headers["Location"]

    def test_s3_presigned_failure_falls_back_generic(self, app):
        with app.test_request_context():
            from app.services.image import serve_image
            from app.services.storage import S3StorageBackend
            with patch("app.services.image.get_storage_backend") as mock_b:
                backend = MagicMock(spec=S3StorageBackend)
                backend.exists.return_value = False
                backend.generate_presigned_url.side_effect = Exception("s3 down")
                backend.get.return_value = b"fallback"
                mock_b.return_value = backend
                resp = serve_image("vouchers/1/x.jpg", "image/jpeg")
                # 汎用フォールバック
                assert resp.status_code == 200

    def test_generic_backend(self, app):
        with app.test_request_context():
            from app.services.image import serve_image
            with patch("app.services.image.get_storage_backend") as mock_b:
                backend = MagicMock()  # not Local nor S3
                backend.exists.return_value = False
                backend.get.return_value = b"generic-bytes"
                mock_b.return_value = backend
                resp = serve_image("x.jpg", "image/png", file_hash="h")
                assert resp.status_code == 200
                assert resp.headers["ETag"] == '"h"'
