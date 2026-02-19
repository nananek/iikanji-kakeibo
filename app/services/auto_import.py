"""自動取込オーケストレーター"""

import json
import logging
from datetime import datetime, timezone

from app.extensions import db
from app.models.auto_import import AutoImportSource, ProcessedFile, WebhookConfig
from app.models.ai_draft import AIDraft
from app.models.ai_config import UserAIConfig
from app.services.ai_receipt import _get_fernet, analyze_and_suggest
from app.services.sources.webdav import WebDAVProvider
from app.services.notify import send_webhook

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_FILES_PER_SOURCE = 100


def encrypt_credentials(credentials: dict) -> bytes:
    """認証情報を暗号化"""
    return _get_fernet().encrypt(json.dumps(credentials).encode())


def decrypt_credentials(encrypted: bytes) -> dict:
    """認証情報を復号"""
    try:
        return json.loads(_get_fernet().decrypt(encrypted).decode())
    except Exception:
        raise ValueError(
            "認証情報の復号に失敗しました。SECRET_KEYが変更された可能性があります。"
            "インポート元を再登録してください。"
        )


def _build_provider(source: AutoImportSource):
    """AutoImportSource からプロバイダーインスタンスを生成"""
    config = json.loads(source.config_json)
    creds = decrypt_credentials(source.credentials_encrypted)

    if source.provider == "webdav":
        return WebDAVProvider(
            url=config["url"],
            username=config["username"],
            password=creds["password"],
            file_extensions=config.get("file_extensions"),
        )

    raise ValueError(f"未対応のプロバイダー: {source.provider}")


def run_auto_import(user_id: int, dry_run: bool = False) -> dict:
    """指定ユーザーの自動取込を実行する。

    Returns:
        {
            "sources_processed": int,
            "files_found": int,
            "files_new": int,
            "drafts_created": int,
            "errors": list[str],
        }
    """
    stats = {
        "sources_processed": 0,
        "files_found": 0,
        "files_new": 0,
        "drafts_created": 0,
        "errors": [],
    }

    ai_config = UserAIConfig.query.filter_by(user_id=user_id).first()
    if not ai_config:
        stats["errors"].append("AI API設定が未登録です。")
        return stats

    sources = AutoImportSource.query.filter_by(
        user_id=user_id, is_active=True
    ).all()
    if not sources:
        return stats

    for source in sources:
        stats["sources_processed"] += 1
        try:
            _process_source(source, user_id, dry_run, stats)
        except Exception as e:
            msg = f"ソース「{source.name}」: {e}"
            logger.error("Auto-import source error: %s", msg)
            stats["errors"].append(msg)

    if not dry_run:
        _notify_user(user_id, stats)

    return stats


def _process_source(
    source: AutoImportSource,
    user_id: int,
    dry_run: bool,
    stats: dict,
):
    """1つのソースを処理する"""
    provider = _build_provider(source)
    files = provider.list_files()
    if len(files) > MAX_FILES_PER_SOURCE:
        logger.warning(
            "Source '%s' has %d files, limiting to %d",
            source.name, len(files), MAX_FILES_PER_SOURCE,
        )
        files = files[:MAX_FILES_PER_SOURCE]
    stats["files_found"] += len(files)

    for file_info in files:
        # 処理済みチェック
        existing = ProcessedFile.query.filter_by(
            source_id=source.id,
            file_path=file_info.path,
        ).first()
        if existing and existing.etag == file_info.etag:
            continue

        stats["files_new"] += 1

        # バリデーション
        if file_info.size > MAX_IMAGE_SIZE:
            logger.warning("File too large, skipping: %s (%d bytes)", file_info.path, file_info.size)
            continue
        mime_type = file_info.mime_type or "image/jpeg"
        if mime_type not in ALLOWED_MIME_TYPES:
            logger.warning("Unsupported MIME type, skipping: %s (%s)", file_info.path, mime_type)
            continue

        _process_file(source, provider, file_info, user_id, mime_type, dry_run, stats)


def _process_file(source, provider, file_info, user_id, mime_type, dry_run, stats):
    """1つのファイルを処理する"""
    try:
        image_bytes = provider.download_file(file_info.path)
        suggestions = analyze_and_suggest(
            user_id, image_bytes, mime_type,
            comment=f"自動取込: {source.name}",
        )

        if dry_run:
            stats["drafts_created"] += 1
            return

        suggestions_json = json.dumps(
            [
                {
                    "title": s.title,
                    "description": s.description,
                    "date": s.date,
                    "entry_description": s.entry_description,
                    "lines": s.lines,
                }
                for s in suggestions
            ],
            ensure_ascii=False,
        )

        draft = AIDraft(
            user_id=user_id,
            image_data=image_bytes,
            image_mime=mime_type,
            comment=f"自動取込: {source.name}",
            suggestions_json=suggestions_json,
            status="analyzed",
        )
        db.session.add(draft)
        db.session.flush()

        pf = ProcessedFile(
            source_id=source.id,
            file_path=file_info.path,
            etag=file_info.etag,
            draft_id=draft.id,
            status="success",
        )
        db.session.add(pf)
        db.session.commit()

        stats["drafts_created"] += 1
        logger.info("Auto-imported: %s", file_info.path)

    except Exception as e:
        db.session.rollback()
        logger.error("Failed to process %s: %s", file_info.path, e)
        stats["errors"].append(f"{file_info.path}: {e}")

        try:
            pf = ProcessedFile(
                source_id=source.id,
                file_path=file_info.path,
                etag=file_info.etag,
                status="error",
                error_message=str(e)[:500],
            )
            db.session.add(pf)
            db.session.commit()
        except Exception:
            db.session.rollback()


def _notify_user(user_id: int, stats: dict):
    """ユーザーの Webhook 通知を送信"""
    from flask import current_app

    webhooks = WebhookConfig.query.filter_by(
        user_id=user_id, is_active=True
    ).all()

    base_url = current_app.config.get("WEBAUTHN_ORIGIN", "").rstrip("/")
    drafts_url = f"{base_url}/ai-journal/drafts" if base_url else None

    for webhook in webhooks:
        events = json.loads(webhook.events_json)

        should_notify = False
        if "import_success" in events and stats["drafts_created"] > 0:
            should_notify = True
        if "import_error" in events and stats["errors"]:
            should_notify = True

        if not should_notify:
            continue

        parts = []
        if stats["drafts_created"] > 0:
            parts.append(f"{stats['drafts_created']}件の下書きを作成しました")
        if stats["errors"]:
            parts.append(f"{len(stats['errors'])}件のエラーが発生しました")
        message = "。".join(parts) + "。"

        send_webhook(
            provider=webhook.provider,
            url=webhook.webhook_url,
            title="いいかんじ™家計簿 自動取込",
            message=message,
            details={
                "検出ファイル": stats["files_found"],
                "新規": stats["files_new"],
                "下書き作成": stats["drafts_created"],
            },
            link_url=drafts_url,
        )
