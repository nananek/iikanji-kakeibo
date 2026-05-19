"""証憑ヘルパー — Voucher 作成・管理"""

import hashlib
import json

from app.extensions import db
from app.models.user import User
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.models.ai_draft import AIDraft
from app.services.storage import (
    get_storage_backend,
    make_storage_key,
    make_thumbnail_key,
    store_image_with_thumbnail,
)
from app.services.storage_quota import (
    QuotaExceededError,
    check_quota,
    get_quota_bytes,
    get_used_bytes,
    maybe_send_quota_warning,
    record_upload,
)


def create_voucher_from_draft(draft: AIDraft, journal_entry_id: int) -> Voucher:
    """AIDraft から Voucher を作成し、ドラフトを削除する。

    画像ファイルはストレージに残し、Voucher が参照を引き継ぐ。
    呼び出し元で db.session.commit() すること。

    容量計上 (Phase 5 #70): AIDraft 生成時に `record_upload` 済のため、
    本関数では **計上量を変更しない** (`record_delete` も `record_upload`
    も呼ばない)。AIDraft → Voucher への所有権移転として `file_size` を
    そのまま引き継ぐ。AIDraft.file_size が NULL の場合 (Phase 5 計上
    開始前のレガシー) は Voucher.file_size も NULL のまま — 整合性
    監査バッチでストレージから実測して埋める。
    """
    voucher = Voucher(
        user_id=draft.user_id,
        journal_entry_id=journal_entry_id,
        image_key=draft.image_key,
        image_mime=draft.image_mime,
        file_hash=draft.file_hash,
        file_size=draft.file_size,
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

    `QuotaExceededError` 送出時は本関数内でストレージ/DB の副作用を
    全て巻き戻してから raise するため、呼び出し側は HTTP エラー (413)
    を返すだけでよい。

    フロー (Phase 5 #70 / 単一トランザクション + ON CONFLICT upsert):

    1. `check_quota(user, len(image_bytes))` で事前判定
    2. Voucher + VoucherAuditLog を session.add (commit せず)
    3. ストレージへ画像書き込み (DB と独立、巻き戻しは best-effort delete)
    4. `record_upload(suppress_commit=True)` で StorageUsage を加算
    5. flush して `get_used_bytes` で TOCTOU 再検証
    6. 上限超過なら `db.session.rollback()` で DB 変更を全部巻き戻し +
       ストレージファイルを best-effort で削除 + `QuotaExceededError`
    7. OK なら `db.session.commit()` で単一トランザクションを確定

    旧パターン (2 段 commit + 巻き戻し失敗時のゾンビ修復) は退役。
    `record_upload` の ON CONFLICT 化により、初回並行 INSERT の競合
    も発生しない。
    """
    size = len(image_bytes)
    user = db.session.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")

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

    # ON CONFLICT upsert で StorageUsage を加算 (commit せず単一 tx 内に保持)
    record_upload(user, size, suppress_commit=True)
    db.session.flush()

    # 楽観的再検証: 並行アップロードで合算が上限超過なら全 DB 変更を rollback
    if get_used_bytes(user) > get_quota_bytes(user):
        from flask import current_app
        db.session.rollback()  # Voucher / AuditLog / StorageUsage 加算を全部巻き戻し
        # ストレージは別系統なので明示的に削除 (best-effort)
        backend = get_storage_backend()
        for k in (key, make_thumbnail_key(key)):
            try:
                backend.delete(k)
            except Exception as e:
                current_app.logger.warning(
                    "voucher rollback: failed to delete storage key %s: %s",
                    k, e,
                )
        raise QuotaExceededError(
            "並行アップロードにより容量上限を超えました。再試行してください。"
        )

    db.session.commit()
    # 容量警告メール送信 (Phase 6 #71)。閾値超過時のみ送信、失敗は best-effort
    maybe_send_quota_warning(user)
    return voucher
