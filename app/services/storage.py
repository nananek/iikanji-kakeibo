"""証憑画像ストレージサービス

ローカルファイルシステムと S3 互換ストレージを設定で切り替え可能にする。
"""

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

from flask import current_app

logger = logging.getLogger(__name__)

MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def make_storage_key(user_id: int, draft_id: int, mime_type: str) -> str:
    """ストレージキーを生成する。

    フォーマット: vouchers/{user_id}/{draft_id}.{ext}
    """
    ext = MIME_TO_EXT.get(mime_type, "bin")
    return f"vouchers/{user_id}/{draft_id}.{ext}"


class StorageBackend(ABC):
    """ストレージバックエンドの抽象基底クラス"""

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None:
        """データを保存する"""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """データを取得する。見つからない場合は FileNotFoundError"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """データを削除する。存在しなくてもエラーにしない"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """キーが存在するか確認する"""


class LocalStorageBackend(StorageBackend):
    """ローカルファイルシステムバックエンド"""

    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)

    def _full_path(self, key: str) -> Path:
        resolved = (self._base_dir / key).resolve()
        if not str(resolved).startswith(str(self._base_dir.resolve())):
            raise ValueError(f"Invalid storage key: {key}")
        return resolved

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._full_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        logger.debug("Stored %s (%d bytes)", key, len(data))

    def get(self, key: str) -> bytes:
        path = self._full_path(key)
        if not path.exists():
            raise FileNotFoundError(f"Storage key not found: {key}")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        path = self._full_path(key)
        if path.exists():
            path.unlink()
            logger.debug("Deleted %s", key)

    def exists(self, key: str) -> bool:
        return self._full_path(key).exists()


class S3StorageBackend(StorageBackend):
    """S3互換ストレージバックエンド (AWS S3 / MinIO / Cloudflare R2)"""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ):
        import boto3

        kwargs: dict = {}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if region:
            kwargs["region_name"] = region
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key

        self._client = boto3.client("s3", **kwargs)
        self._bucket = bucket

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.debug("S3 put %s (%d bytes)", key, len(data))

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(
                Bucket=self._bucket, Key=key
            )
            return response["Body"].read()
        except self._client.exceptions.NoSuchKey:
            raise FileNotFoundError(f"S3 key not found: {key}")

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)
        logger.debug("S3 deleted %s", key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False


# --- ファクトリ (シングルトンキャッシュ) ---

_backend_instance: StorageBackend | None = None


def get_storage_backend() -> StorageBackend:
    """アプリ設定に基づいてストレージバックエンドを返す。"""
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    backend_type = current_app.config.get("STORAGE_BACKEND", "local")

    if backend_type == "s3":
        _backend_instance = S3StorageBackend(
            bucket=current_app.config["STORAGE_S3_BUCKET"],
            endpoint_url=current_app.config.get("STORAGE_S3_ENDPOINT"),
            region=current_app.config.get("STORAGE_S3_REGION"),
            access_key=current_app.config.get("STORAGE_S3_ACCESS_KEY"),
            secret_key=current_app.config.get("STORAGE_S3_SECRET_KEY"),
        )
    else:
        base_dir = current_app.config.get(
            "STORAGE_LOCAL_DIR", "/app/data/vouchers"
        )
        _backend_instance = LocalStorageBackend(base_dir)

    return _backend_instance


def reset_storage_backend() -> None:
    """テスト用: キャッシュをリセットする"""
    global _backend_instance
    _backend_instance = None
