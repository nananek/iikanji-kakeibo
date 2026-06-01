"""証憑ヘルパー — Voucher 作成・管理"""

import hashlib
import secrets

from sqlalchemy.exc import IntegrityError

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


# aad_id は 63bit ランダム正整数 (Postgres BIGINT = signed 64bit に収まる)。
_AAD_ID_BITS = 63
_AAD_ID_MAX_RETRIES = 5


def _generate_unique_aad_id(user_id: int) -> int:
    """voucher の AAD 束縛用に (user_id, aad_id) で一意な 63bit ランダムを生成。

    UNIQUE(user_id, aad_id) 制約に対し SAVEPOINT + リトライで衝突を吸収する
    (衝突確率は 2^-63 で実質発生しないが、制約違反でリクエストを落とさない
    ための保険)。呼び出し元は新規 Voucher にこの値をセットして flush すること。
    """
    return secrets.randbits(_AAD_ID_BITS) or 1


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


def init_voucher(user_id: int, journal_entry_id: int | None) -> Voucher:
    """E4 (#111) 2 段階 upload の Step 1: 空の Voucher を作成して id を採番する。

    画像本体は採番された voucher_id を AAD に束縛してクライアントが暗号化する
    ため、実体 upload 前に id が必要。本関数は空 row (image_key="") を作るだけで
    容量計上は行わない (実体 upload = `finalize_voucher_upload` で計上する)。

    中断 (init したが PUT されない) すると image_key="" の空 row が残る。
    一覧/配信は image_key が空なら表示しないため実害はなく、整合性監査バッチ
    (storage-audit 系) で回収する想定。呼び出し元で db.session.commit() すること。

    E4 (#111) Option C: AAD 束縛用の安定識別子 `aad_id` を生成して row に
    セットする。クライアントは init レスポンスの aad_id を AAD (vimg/vthumb/
    vmeta) に束縛して暗号化する。
    """
    for attempt in range(_AAD_ID_MAX_RETRIES):
        aad_id = _generate_unique_aad_id(user_id)
        sp = db.session.begin_nested()
        voucher = Voucher(
            user_id=user_id,
            journal_entry_id=journal_entry_id,
            image_key="",
            # 暗号化証憑の実 MIME は encrypted_meta_blob に入る。image_mime 列は
            # NOT NULL 制約のためプレースホルダを入れる。列の DROP は AI 下書き
            # E2EE 化後の後続 PR に延期 (AI クイックアクセプトが依然平文 voucher
            # を生成し配信に image_mime を使うため)。
            image_mime=ENCRYPTED_CONTENT_TYPE,
            aad_id=aad_id,
        )
        db.session.add(voucher)
        try:
            db.session.flush()
            sp.commit()
            return voucher
        except IntegrityError:
            # (user_id, aad_id) 衝突 → SAVEPOINT を巻き戻して別値で再試行。
            sp.rollback()
    raise RuntimeError(
        "init_voucher: aad_id の一意生成に失敗しました (リトライ上限)。"
    )


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

    容量計上 (Phase 5 #70) は単一トランザクション + 楽観的再検証パターン
    (check_quota 事前判定 → record_upload(suppress_commit=True) で加算 →
    flush 後に get_used_bytes で TOCTOU 再検証 → 超過なら rollback +
    ストレージ best-effort delete)。暗号文サイズ (image_ct + thumb_ct) を計上する。
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
