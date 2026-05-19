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

    `QuotaExceededError` 送出時は本関数内でストレージ/DB の副作用を
    全て巻き戻してから raise するため、呼び出し側は HTTP エラー (413)
    を返すだけでよい。

    フロー (Phase 5 #70 / TOCTOU 楽観的再検証パターン):

    1. `check_quota(user, len(image_bytes))` で事前判定
    2. Voucher を DB に追加 + ストレージへ画像書き込み
    3. `record_upload(user, size)` で容量加算 (アトミック UPDATE)
    4. 並行アップロードと合算して上限超過なら巻き戻し + 例外

    NOTE: `record_upload` の内部 `db.session.commit()` により、戻り値を
    受け取った時点で Voucher は既に永続化されている。呼び出し元の
    `db.session.commit()` は冪等な操作になる
    (`create_voucher_from_draft` は内部 commit を行わないため非対称)。
    Phase 5 続編で `record_upload`/`record_delete` の `suppress_commit`
    対応時に解消予定。
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

    # 4. 楽観的再検証: 並行アップロードで合算が上限超過なら巻き戻し。
    #    `record_upload` 内で commit 済のため、Voucher / VoucherAuditLog
    #    も永続化されている。これらを明示的に削除して commit し直す。
    if get_used_bytes(user) > get_quota_bytes(user):
        from flask import current_app
        # 巻き戻し順序:
        # ① ストレージ削除 (best-effort)
        # ② VoucherAuditLog + Voucher を 1 トランザクションで delete
        # ③ record_delete で StorageUsage 減算 (内部で commit)
        # ②の commit が失敗した場合、`db.session.delete(voucher)` も
        # ロールバックされて Voucher / VoucherAuditLog は DB に残り、
        # record_delete も走らないので StorageUsage も過剰計上で残る
        # (= 両方ゾンビ状態)。次回の整合性監査バッチで検出・修正される
        # 前提。Phase 5 続編で `record_upload`/`record_delete` の commit
        # 制御 (suppress_commit) を入れて単一トランザクション化し、
        # この問題を根本解決する予定。
        backend = get_storage_backend()
        for k in (key, make_thumbnail_key(key)):
            try:
                backend.delete(k)
            except Exception as e:
                current_app.logger.warning(
                    "voucher rollback: failed to delete storage key %s: %s",
                    k, e,
                )
        # VoucherAuditLog は voucher_id FK (ondelete 未指定 = RESTRICT) を
        # 持つため、Voucher 削除前に AuditLog を先に削除する必要がある
        # (PostgreSQL では IntegrityError 防止、SQLite でも安全)。
        VoucherAuditLog.query.filter_by(voucher_id=voucher.id).delete()
        db.session.delete(voucher)
        db.session.commit()
        # record_delete が例外を投げても呼出側 (attach view) が
        # QuotaExceededError を catch できるよう保護する。
        # StorageUsage の過剰計上分は整合性監査バッチで検出・修正される
        # 前提 (次 PR で suppress_commit 単一トランザクション化で根治)。
        try:
            record_delete(user, size)
        except Exception as e:
            current_app.logger.exception(
                "rollback: record_delete failed for user_id=%d size=%d: %s",
                user.id, size, e,
            )
        raise QuotaExceededError(
            "並行アップロードにより容量上限を超えました。再試行してください。"
        )

    return voucher
