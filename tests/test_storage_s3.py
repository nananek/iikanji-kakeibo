"""S3 ストレージバックエンド (services/storage.py) のテスト

boto3 クライアントをモック化して全メソッドをカバー。
"""

from unittest.mock import MagicMock, patch

import pytest


class TestS3Backend:
    def _make_backend(self, mock_boto3_client):
        from app.services.storage import S3StorageBackend
        with patch("boto3.client") as mock_client:
            mock_client.return_value = mock_boto3_client
            backend = S3StorageBackend(bucket="test-bucket")
        return backend

    def test_init_basic(self, app):
        with app.app_context():
            mock_client = MagicMock()
            from app.services.storage import S3StorageBackend
            with patch("boto3.client") as mock_boto3:
                mock_boto3.return_value = mock_client
                backend = S3StorageBackend(bucket="b")
                mock_boto3.assert_called_once_with("s3")
                assert backend._bucket == "b"

    def test_init_with_full_config(self, app):
        with app.app_context():
            from app.services.storage import S3StorageBackend
            with patch("boto3.client") as mock_boto3:
                mock_boto3.return_value = MagicMock()
                S3StorageBackend(
                    bucket="b",
                    endpoint_url="https://minio.local",
                    region="us-west-1",
                    access_key="ak",
                    secret_key="sk",
                )
                kwargs = mock_boto3.call_args.kwargs
                assert kwargs["endpoint_url"] == "https://minio.local"
                assert kwargs["region_name"] == "us-west-1"
                assert kwargs["aws_access_key_id"] == "ak"
                assert kwargs["aws_secret_access_key"] == "sk"

    def test_put(self, app):
        with app.app_context():
            mock_client = MagicMock()
            backend = self._make_backend(mock_client)
            backend.put("k.jpg", b"data", "image/jpeg")
            mock_client.put_object.assert_called_once_with(
                Bucket="test-bucket", Key="k.jpg",
                Body=b"data", ContentType="image/jpeg",
            )

    def test_get_success(self, app):
        with app.app_context():
            mock_client = MagicMock()
            mock_body = MagicMock()
            mock_body.read.return_value = b"image-data"
            mock_client.get_object.return_value = {"Body": mock_body}
            backend = self._make_backend(mock_client)
            result = backend.get("k.jpg")
            assert result == b"image-data"

    def test_get_not_found(self, app):
        with app.app_context():
            mock_client = MagicMock()
            # NoSuchKey 例外
            class NoSuchKey(Exception):
                pass
            mock_client.exceptions.NoSuchKey = NoSuchKey
            mock_client.get_object.side_effect = NoSuchKey()
            backend = self._make_backend(mock_client)
            with pytest.raises(FileNotFoundError):
                backend.get("missing.jpg")

    def test_delete(self, app):
        with app.app_context():
            mock_client = MagicMock()
            backend = self._make_backend(mock_client)
            backend.delete("k.jpg")
            mock_client.delete_object.assert_called_once_with(
                Bucket="test-bucket", Key="k.jpg",
            )

    def test_exists_yes(self, app):
        with app.app_context():
            mock_client = MagicMock()
            backend = self._make_backend(mock_client)
            assert backend.exists("k.jpg") is True

    def test_exists_no(self, app):
        with app.app_context():
            mock_client = MagicMock()
            mock_client.head_object.side_effect = Exception("not found")
            backend = self._make_backend(mock_client)
            assert backend.exists("k.jpg") is False

    def test_generate_presigned_url(self, app):
        with app.app_context():
            mock_client = MagicMock()
            mock_client.generate_presigned_url.return_value = "https://s3.example/x?sig"
            backend = self._make_backend(mock_client)
            url = backend.generate_presigned_url("k.jpg", expires_in=600)
            assert url == "https://s3.example/x?sig"
            kwargs = mock_client.generate_presigned_url.call_args.kwargs
            assert kwargs["ExpiresIn"] == 600


class TestGetStorageBackendFactory:
    def test_default_local(self, app):
        # _backend_instance のキャッシュをクリア
        import app.services.storage as storage
        storage._backend_instance = None
        with app.app_context():
            app.config["STORAGE_BACKEND"] = "local"
            try:
                backend = storage.get_storage_backend()
                from app.services.storage import LocalStorageBackend
                assert isinstance(backend, LocalStorageBackend)
            finally:
                storage._backend_instance = None

    def test_s3_backend(self, app):
        import app.services.storage as storage
        storage._backend_instance = None
        with app.app_context():
            app.config["STORAGE_BACKEND"] = "s3"
            app.config["STORAGE_S3_BUCKET"] = "my-bucket"
            app.config["STORAGE_S3_ENDPOINT"] = None
            app.config["STORAGE_S3_REGION"] = "us-east-1"
            try:
                with patch("boto3.client") as mock_boto3:
                    mock_boto3.return_value = MagicMock()
                    backend = storage.get_storage_backend()
                    from app.services.storage import S3StorageBackend
                    assert isinstance(backend, S3StorageBackend)
            finally:
                storage._backend_instance = None
                app.config["STORAGE_BACKEND"] = "local"

    def test_singleton_cache(self, app):
        import app.services.storage as storage
        storage._backend_instance = None
        with app.app_context():
            app.config["STORAGE_BACKEND"] = "local"
            try:
                b1 = storage.get_storage_backend()
                b2 = storage.get_storage_backend()
                assert b1 is b2
            finally:
                storage._backend_instance = None
