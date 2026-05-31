"""証憑ヘルパー — Voucher 作成・管理"""

import hashlib

from app.extensions import db
from app.models.user import User
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.models.ai_draft import AIDraft
from app.services.storage import (
    ENCRYPTED_CONTENT_TYPE,
    get_storage_backend,
    make_encrypted_thumbnail_key,
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


class VoucherUploadConflict(Exception):
    """E4 (#111): 既に確定済みの Voucher への二重 upload (並行 PUT)。

    呼び出し側 (PUT エンドポイント) は 409 に変換する。"""


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

    # E4 PR-D: 平文 detail は書かない。紐付け先 (journal_entry_id) は
    # voucher.journal_entry_id 列に保持されており冗長。action + voucher_id +
    # created_at で証跡は足りる (encrypted_detail_blob はクライアント供給の
    # 暗号化ノート用に予約、valog AAD)。
    db.session.add(VoucherAuditLog(
        voucher_id=voucher.id,
        user_id=user_id,
        action="attached",
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


def init_voucher(user_id: int, journal_entry_id: int | None) -> Voucher:
    """E4 (#111) 2 段階 upload の Step 1: 空の Voucher を作成して id を採番する。

    画像本体は採番された voucher_id を AAD に束縛してクライアントが暗号化する
    ため、実体 upload 前に id が必要。本関数は空 row (image_key="") を作るだけで
    容量計上は行わない (実体 upload = `finalize_voucher_upload` で計上する)。

    中断 (init したが PUT されない) すると image_key="" の空 row が残る。
    一覧/配信は image_key が空なら表示しないため実害はなく、整合性監査バッチ
    (storage-audit 系) で回収する想定。呼び出し元で db.session.commit() すること。
    """
    voucher = Voucher(
        user_id=user_id,
        journal_entry_id=journal_entry_id,
        image_key="",
        # 暗号化証憑の実 MIME は encrypted_meta_blob に入る。列は dual-write 期
        # の間だけ NOT NULL 制約のためプレースホルダを入れる (057 で DROP)。
        image_mime=ENCRYPTED_CONTENT_TYPE,
    )
    db.session.add(voucher)
    db.session.flush()
    return voucher


def finalize_voucher_upload(
    voucher: Voucher,
    image_ct: bytes,
    thumb_ct: bytes | None,
    encrypted_meta_blob: bytes,
    meta_iv: bytes,
    file_hash_plain: str,
) -> Voucher:
    """E4 (#111) 2 段階 upload の Step 2: 暗号文の実体を保存して確定する。

    `image_ct` / `thumb_ct` はクライアントが `iv(12B) || ciphertext || tag` を
    連結した opaque バイト列 (画像/サムネ本体はストレージに保存するため IV は
    DB 列ではなく blob 先頭に inline)。サーバは中身を一切復号できない。

    - file_hash_cipher = SHA-256(image_ct) を `voucher.file_hash` に保存
      (サーバが MK なしで「保存した暗号文が改ざんされていないか」を検証する
      電帳法 Q11 ハイブリッドの cipher 側)。
    - file_hash_plain (= SHA-256(平文画像)、クライアント計算) はそのまま保存。
    - encrypted_meta_blob / meta_iv (original_filename + image_mime 等) を保存。

    容量計上 (Phase 5 #70) は `create_voucher_from_upload` と同じ単一トランザク
    ション + 楽観的再検証パターン。暗号文サイズ (image_ct + thumb_ct) を計上する。
    `QuotaExceededError` 送出時はストレージ/DB の副作用を巻き戻してから raise。

    呼び出し元で 上書き防止 (既に確定済みでないこと) を検証済みであること。
    """
    size = len(image_ct) + (len(thumb_ct) if thumb_ct else 0)
    user = db.session.get(User, voucher.user_id)
    if user is None:
        raise ValueError(f"User {voucher.user_id} not found")

    check_quota(user, size)

    image_key = make_storage_key(
        voucher.user_id, voucher.id, ENCRYPTED_CONTENT_TYPE
    )

    # 原子的クレーム (PR-B レビュー指摘 ①): 並行 PUT が両方とも endpoint の
    # 楽観チェック (image_key=="") を通過した場合でも、ここで image_key を
    # WHERE image_key='' 条件付き UPDATE で確定し、勝者のみが先へ進む。
    # 敗者は rowcount=0 → VoucherUploadConflict (= 409) で、ストレージ書き込み
    # の前に弾く (電帳法の上書き禁止を DB レベルで保証)。
    claimed = (
        db.session.query(Voucher)
        .filter(
            Voucher.id == voucher.id,
            Voucher.image_key == "",
            Voucher.encrypted_meta_blob.is_(None),
        )
        .update(
            {Voucher.image_key: image_key},
            synchronize_session=False,
        )
    )
    if claimed != 1:
        db.session.rollback()
        raise VoucherUploadConflict(
            "この証憑は既にアップロード済みです。"
        )

    backend = get_storage_backend()
    backend.put(image_key, image_ct, ENCRYPTED_CONTENT_TYPE)

    thumbnail_key = None
    if thumb_ct:
        thumbnail_key = make_encrypted_thumbnail_key(image_key)
        backend.put(thumbnail_key, thumb_ct, ENCRYPTED_CONTENT_TYPE)

    voucher.image_key = image_key
    voucher.thumbnail_key = thumbnail_key
    voucher.encrypted_meta_blob = encrypted_meta_blob
    voucher.meta_iv = meta_iv
    voucher.file_hash = hashlib.sha256(image_ct).hexdigest()  # cipher hash
    voucher.file_hash_plain = file_hash_plain
    voucher.file_size = size

    # E4 PR-D: 平文 detail は書かない (journal_entry_id は voucher 行に保持済で
    # 冗長)。encrypted_detail_blob はクライアント供給の暗号化ノート用に予約。
    db.session.add(VoucherAuditLog(
        voucher_id=voucher.id,
        user_id=voucher.user_id,
        action="attached",
    ))

    record_upload(user, size, suppress_commit=True)
    db.session.flush()

    if get_used_bytes(user) > get_quota_bytes(user):
        from flask import current_app
        db.session.rollback()
        keys = [image_key]
        if thumbnail_key:
            keys.append(thumbnail_key)
        for k in keys:
            try:
                backend.delete(k)
            except Exception as e:
                current_app.logger.warning(
                    "voucher finalize rollback: failed to delete %s: %s", k, e,
                )
        raise QuotaExceededError(
            "並行アップロードにより容量上限を超えました。再試行してください。"
        )

    db.session.commit()
    maybe_send_quota_warning(user)
    return voucher
