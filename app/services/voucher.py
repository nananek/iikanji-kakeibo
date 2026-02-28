"""証憑ヘルパー — Voucher 作成・管理"""

import hashlib
import json

from app.extensions import db
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.models.ai_draft import AIDraft
from app.services.storage import make_storage_key, store_image_with_thumbnail


def create_voucher_from_draft(draft: AIDraft, journal_entry_id: int) -> Voucher:
    """AIDraft から Voucher を作成し、ドラフトを削除する。

    画像ファイルはストレージに残し、Voucher が参照を引き継ぐ。
    呼び出し元で db.session.commit() すること。
    """
    voucher = Voucher(
        user_id=draft.user_id,
        journal_entry_id=journal_entry_id,
        image_key=draft.image_key,
        image_mime=draft.image_mime,
        file_hash=draft.file_hash,
        uploaded_at=draft.created_at,
    )
    db.session.add(voucher)
    db.session.delete(draft)
    return voucher


def create_voucher_from_upload(
    user_id: int,
    journal_entry_id: int,
    image_bytes: bytes,
    mime_type: str,
    original_filename: str | None = None,
) -> Voucher:
    """画像バイト列から直接 Voucher を作成して仕訳に紐付ける。

    呼び出し元で db.session.commit() すること。
    """
    file_hash = hashlib.sha256(image_bytes).hexdigest()

    voucher = Voucher(
        user_id=user_id,
        journal_entry_id=journal_entry_id,
        image_key="",
        image_mime=mime_type,
        file_hash=file_hash,
        original_filename=original_filename,
    )
    db.session.add(voucher)
    db.session.flush()

    key = make_storage_key(user_id, voucher.id, mime_type)
    store_image_with_thumbnail(key, image_bytes, mime_type)
    voucher.image_key = key

    db.session.add(VoucherAuditLog(
        voucher_id=voucher.id,
        user_id=user_id,
        action="attached",
        detail=json.dumps(
            {"journal_entry_id": journal_entry_id},
            ensure_ascii=False,
        ),
    ))

    return voucher
