"""証憑ヘルパー — Voucher 作成・管理"""

import hashlib
import json

from app.extensions import db
from app.models.user import User
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.models.ai_draft import AIDraft
from app.services.storage import make_storage_key, store_image_with_thumbnail
from app.services.storage_quota import (
    QuotaExceededError,
    check_quota,
    get_quota_bytes,
    get_used_bytes,
    record_delete,
    record_upload,
)


def create_voucher_from_draft(draft: AIDraft, journal_entry_id: int) -> Voucher:
    """AIDraft から Voucher を作成し、ドラフトを削除する。

    画像ファイルはストレージに残し、Voucher が参照を引き継ぐ。
    呼び出し元で db.session.commit() すること。

    NOTE: 本関数はまだ容量計上 (`check_quota`/`record_upload`) を統合
    していない。Phase 5 続編 PR で AIDraft 経由のフローも quota 管理下
    に置く予定 (AIDraft 段階で一時保管されているサイズを Voucher 化
    時に永続化として計上する設計)。
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

    呼び出し元で `db.session.commit()` すること。`QuotaExceededError`
    送出時は本関数内でストレージ/DB の副作用を全て巻き戻してから
    raise するため、呼び出し側は HTTP エラー (413) を返すだけでよい。

    フロー (Phase 5 #70 / TOCTOU 楽観的再検証パターン):

    1. `check_quota(user, len(image_bytes))` で事前判定
    2. Voucher を DB に追加 + ストレージへ画像書き込み
    3. `record_upload(user, size)` で容量加算 (アトミック UPDATE)
    4. 並行アップロードと合算して上限超過なら巻き戻し + 例外
    """
    size = len(image_bytes)
    user = db.session.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")

    # 1. 事前チェック
    check_quota(user, size)

    file_hash = hashlib.sha256(image_bytes).hexdigest()

    voucher = Voucher(
        user_id=user_id,
        journal_entry_id=journal_entry_id,
        image_key="",
        image_mime=mime_type,
        file_hash=file_hash,
        file_size=size,
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

    # 3. 容量加算 (アトミック UPDATE)
    record_upload(user, size)

    # 4. 楽観的再検証: 並行アップロードで合算が上限超過なら巻き戻し
    if get_used_bytes(user) > get_quota_bytes(user):
        record_delete(user, size)
        # ストレージファイルと Voucher 行を撤去
        from app.services.storage import get_storage_backend, make_thumbnail_key
        backend = get_storage_backend()
        for k in (key, make_thumbnail_key(key)):
            try:
                backend.delete(k)
            except Exception:
                pass
        db.session.delete(voucher)
        db.session.flush()
        raise QuotaExceededError(
            "並行アップロードにより容量上限を超えました。再試行してください。"
        )

    return voucher
