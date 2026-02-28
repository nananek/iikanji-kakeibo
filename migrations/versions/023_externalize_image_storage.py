"""Externalize voucher image storage from DB to filesystem/S3

Revision ID: 023_externalize_image_storage
Revises: 022_ai_config_base_url
Create Date: 2026-02-28
"""

import logging
import os
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "023_externalize_image_storage"
down_revision = "022_ai_config_base_url"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)

MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


# --- ボリュームマウント安全チェック ---


def _is_docker_environment() -> bool:
    """Docker コンテナ内で動作しているか判定"""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def _is_volume_mounted(path: str) -> bool:
    """指定パスがボリュームマウントされているか確認する。

    /proc/mounts を解析して、path またはその祖先が
    / や /app 以外のマウントポイントかを確認。
    """
    target = os.path.realpath(path)

    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mount_point = parts[1]
                if mount_point in ("/", "/app"):
                    continue
                if target == mount_point or target.startswith(mount_point + "/"):
                    return True
    except (OSError, IOError):
        pass

    return False


def _check_storage_safety(storage_dir: str) -> None:
    """ストレージの安全性を検証する。

    Docker環境でローカルファイルシステムを使う場合、
    対象ディレクトリがボリュームマウントされていないと
    コンテナ削除時にデータが失われる。
    """
    backend = os.environ.get("STORAGE_BACKEND", "local")

    if backend != "local":
        logger.info("Storage backend is '%s', skipping volume mount check.", backend)
        return

    if not _is_docker_environment():
        logger.warning(
            "Non-Docker environment detected. "
            "Ensure '%s' is a persistent directory before production use.",
            storage_dir,
        )
        return

    if not _is_volume_mounted(storage_dir):
        raise RuntimeError(
            f"\n"
            f"=== 証憑画像ストレージの安全チェック失敗 ===\n"
            f"\n"
            f"ストレージディレクトリ '{storage_dir}' がボリュームマウントされていません。\n"
            f"このままマイグレーションを実行すると、コンテナ削除時に証憑画像が\n"
            f"全て失われます。\n"
            f"\n"
            f"docker-compose.yml の web サービスに以下を追加してください:\n"
            f"\n"
            f"  volumes:\n"
            f"    - ./volumes/vouchers:{storage_dir}\n"
            f"\n"
            f"追加後、コンテナを再作成してから再実行してください。\n"
            f"=== マイグレーションを中断します ==="
        )

    logger.info(
        "Volume mount check passed: '%s' is properly mounted.", storage_dir
    )


# --- データ移行ヘルパー ---


def _migrate_to_local(rows, storage_dir: str):
    """既存画像をローカルファイルシステムに書き出す"""
    base = Path(storage_dir)
    for row in rows:
        ext = MIME_TO_EXT.get(row.image_mime, "bin")
        key = f"vouchers/{row.user_id}/{row.id}.{ext}"
        path = base / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(row.image_data)
        logger.debug("Wrote %s (%d bytes)", key, len(row.image_data))


def _migrate_to_s3(rows):
    """既存画像をS3互換ストレージにアップロードする"""
    import boto3

    kwargs: dict = {}
    endpoint = os.environ.get("STORAGE_S3_ENDPOINT")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    region = os.environ.get("STORAGE_S3_REGION")
    if region:
        kwargs["region_name"] = region
    access_key = os.environ.get("STORAGE_S3_ACCESS_KEY")
    secret_key = os.environ.get("STORAGE_S3_SECRET_KEY")
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key

    client = boto3.client("s3", **kwargs)
    bucket = os.environ.get("STORAGE_S3_BUCKET")

    for row in rows:
        ext = MIME_TO_EXT.get(row.image_mime, "bin")
        key = f"vouchers/{row.user_id}/{row.id}.{ext}"
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=row.image_data,
            ContentType=row.image_mime,
        )
        logger.debug("Uploaded %s to S3 (%d bytes)", key, len(row.image_data))


# --- マイグレーション ---


def upgrade():
    storage_dir = os.environ.get("STORAGE_LOCAL_DIR", "/app/data/vouchers")

    # フェーズ1: ストレージ安全チェック
    _check_storage_safety(storage_dir)

    # フェーズ2: image_key カラムを追加
    op.add_column(
        "ai_drafts",
        sa.Column("image_key", sa.String(255), nullable=True),
    )

    # フェーズ3: 既存データの移行
    backend = os.environ.get("STORAGE_BACKEND", "local")
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, user_id, image_data, image_mime "
            "FROM ai_drafts WHERE image_data IS NOT NULL"
        )
    ).fetchall()

    if rows:
        logger.info("Migrating %d image(s) to storage...", len(rows))

        if backend == "s3":
            _migrate_to_s3(rows)
        else:
            _migrate_to_local(rows, storage_dir)

        for row in rows:
            ext = MIME_TO_EXT.get(row.image_mime, "bin")
            key = f"vouchers/{row.user_id}/{row.id}.{ext}"
            conn.execute(
                sa.text(
                    "UPDATE ai_drafts SET image_key = :key WHERE id = :id"
                ),
                {"key": key, "id": row.id},
            )

        logger.info("Image migration completed.")
    else:
        logger.info("No existing images to migrate.")

    # フェーズ4: image_data カラム削除、image_key を NOT NULL に
    op.drop_column("ai_drafts", "image_data")
    op.alter_column(
        "ai_drafts", "image_key",
        existing_type=sa.String(255),
        nullable=False,
    )


def downgrade():
    op.add_column(
        "ai_drafts",
        sa.Column("image_data", sa.LargeBinary(), nullable=True),
    )
    op.drop_column("ai_drafts", "image_key")
