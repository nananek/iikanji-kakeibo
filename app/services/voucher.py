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
    make_ai_draft_encrypted_key,
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

    呼び出し側 (PUT エンドポイント) は 409 に変換する。E5 (#111) の AI 下書き
    2 段階 upload (finalize_ai_draft_upload) でも同じ意味で再利用する。"""


# aad_id は 63bit ランダム正整数 (Postgres BIGINT = signed 64bit に収まる)。
_AAD_ID_BITS = 63
_AAD_ID_MAX_RETRIES = 5
# UNIQUE(user_id, aad_id) 衝突の判定マーカー。Postgres は制約名
# "uq_vouchers_user_aad_id"、SQLite は列名 "vouchers.aad_id" を IntegrityError
# メッセージに含むため、両者に共通して現れる "aad_id" で判定する。これにより
# aad_id 衝突のみリトライし、他制約 (FK / NOT NULL 等) は誤って隠蔽しない。
_AAD_ID_CONFLICT_MARKER = "aad_id"


def _random_aad_id() -> int:
    """voucher の AAD 束縛用 63bit ランダム正整数を生成する。

    一意性は呼び出し元の UNIQUE(user_id, aad_id) 制約 + SAVEPOINT リトライが
    担保する (本関数は値の生成のみ)。0 は避けて [1, 2^63-1] を返す。
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

    E5 (#111): E2EE 化された下書き (encrypted_meta_blob != None) の場合、
    暗号化成果物 (encrypted_meta_blob / meta_iv / file_hash_plain /
    thumbnail_key) と **aad_id** をそのまま Voucher に引き継ぐ。aad_id が
    AAD に束縛されているため、再暗号化せずに image_key の暗号文をそのまま
    証憑として参照でき、クライアントは同じ AAD で復号できる。レガシー平文
    下書きはこれらが全て None なので従来通り平文証憑になる (両対応)。
    """
    voucher = Voucher(
        user_id=draft.user_id,
        journal_entry_id=journal_entry_id,
        image_key=draft.image_key,
        file_hash=draft.file_hash,
        file_size=draft.file_size,
        uploaded_at=draft.created_at,
        # E5 (#111): E2EE 成果物の引き継ぎ (平文下書きでは全て None)。
        encrypted_meta_blob=draft.encrypted_meta_blob,
        meta_iv=draft.meta_iv,
        file_hash_plain=draft.file_hash_plain,
        thumbnail_key=draft.thumbnail_key,
        aad_id=draft.aad_id,
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
        sp = db.session.begin_nested()
        voucher = Voucher(
            user_id=user_id,
            journal_entry_id=journal_entry_id,
            image_key="",
            # E5 PR-5 (#111): image_mime 列は 060 で DROP 済 (実 MIME は
            # encrypted_meta_blob 内)。
            aad_id=_random_aad_id(),
        )
        db.session.add(voucher)
        try:
            db.session.flush()
            sp.commit()
            return voucher
        except IntegrityError as e:
            sp.rollback()
            # aad_id 衝突以外の IntegrityError (FK / NOT NULL 等) は誤解を招く
            # RuntimeError で隠蔽せず、そのまま再送出する。
            marker = str(getattr(e, "orig", None) or e)
            if _AAD_ID_CONFLICT_MARKER not in marker:
                raise
            # (user_id, aad_id) 衝突 → 次のループで別値を試す。
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


def init_ai_draft(user_id: int, comment: str | None) -> AIDraft:
    """E5 (#111) AI 下書き 2 段階 upload の Step 1: 空の AIDraft を採番する。

    画像本体は採番された aad_id を AAD に束縛してクライアントが暗号化するため、
    実体 upload 前に id + aad_id が必要。本関数は空 row (image_key="") を
    status="pending" で作るだけで容量計上は行わない (実体 upload =
    `finalize_ai_draft_upload` で計上する)。

    aad_id は voucher と同じ AAD ドメイン (`vimg`/`vthumb`/`vmeta`) で使われ、
    下書き → 証憑移行 (create_voucher_from_draft) 時に Voucher.aad_id へ
    そのまま引き継ぐことで、暗号化済み画像/サムネ/meta を**再暗号化せずに**
    証憑へ移せる。`UNIQUE(user_id, aad_id)` + SAVEPOINT リトライで一意性を担保
    する (init_voucher と同形式)。呼び出し元で db.session.commit() すること。

    中断 (init したが PUT されない) すると image_key="" の空 row が残る。
    upload 画面の temp/pending クリーンアップと整合性監査バッチで回収する想定。
    """
    for attempt in range(_AAD_ID_MAX_RETRIES):
        sp = db.session.begin_nested()
        draft = AIDraft(
            user_id=user_id,
            image_key="",
            # 暗号化下書きの実 MIME は encrypted_meta_blob 内。image_mime 列は
            # NOT NULL のためプレースホルダ (octet-stream) を入れる。
            image_mime=ENCRYPTED_CONTENT_TYPE,
            status="pending",
            suggestions_json="[]",
            comment=comment or None,
            aad_id=_random_aad_id(),
        )
        db.session.add(draft)
        try:
            db.session.flush()
            sp.commit()
            return draft
        except IntegrityError as e:
            sp.rollback()
            # aad_id 衝突以外の IntegrityError (FK / NOT NULL 等) は隠蔽せず再送出。
            marker = str(getattr(e, "orig", None) or e)
            if _AAD_ID_CONFLICT_MARKER not in marker:
                raise
            # (user_id, aad_id) 衝突 → 次のループで別値を試す。
    raise RuntimeError(
        "init_ai_draft: aad_id の一意生成に失敗しました (リトライ上限)。"
    )


def finalize_ai_draft_upload(
    draft: AIDraft,
    image_ct: bytes,
    thumb_ct: bytes | None,
    encrypted_meta_blob: bytes,
    meta_iv: bytes,
    file_hash_plain: str,
) -> AIDraft:
    """E5 (#111) AI 下書き 2 段階 upload の Step 2: 暗号文の実体を保存して確定する。

    `finalize_voucher_upload` と同形式 (`iv(12B) || ciphertext || tag` の opaque
    バイト列をストレージへ inline 格納、原子的クレーム、TOCTOU 再検証付き容量
    計上)。AIDraft 固有の差分:

    - VoucherAuditLog は記録しない (下書きは電帳法証跡の対象外。証憑化されて
      初めて attached ログが付く)。
    - status は "pending" のまま (クライアントが LLM 解析後に
      PATCH /ai/drafts/<id>/suggestions で "analyzed" に昇格する)。
    - 容量は暗号文サイズ (image_ct + thumb_ct) を計上する。証憑化時
      (create_voucher_from_draft) は file_size を引き継ぐだけで再計上しない。

    呼び出し元で 上書き防止 (既に確定済みでないこと) を検証済みであること。
    """
    size = len(image_ct) + (len(thumb_ct) if thumb_ct else 0)
    user = db.session.get(User, draft.user_id)
    if user is None:
        raise ValueError(f"User {draft.user_id} not found")

    check_quota(user, size)

    image_key = make_ai_draft_encrypted_key(draft.user_id, draft.id)

    # 原子的クレーム (finalize_voucher_upload と同じ): 並行 PUT が両方とも
    # endpoint の楽観チェック (image_key=="") を通過しても、WHERE image_key=''
    # 条件付き UPDATE で勝者のみが先へ進む。敗者は rowcount=0 →
    # VoucherUploadConflict (= 409) でストレージ書き込み前に弾く。
    claimed = (
        db.session.query(AIDraft)
        .filter(
            AIDraft.id == draft.id,
            AIDraft.image_key == "",
            AIDraft.encrypted_meta_blob.is_(None),
        )
        .update(
            {AIDraft.image_key: image_key},
            synchronize_session=False,
        )
    )
    if claimed != 1:
        db.session.rollback()
        raise VoucherUploadConflict(
            "この下書きは既にアップロード済みです。"
        )

    backend = get_storage_backend()
    backend.put(image_key, image_ct, ENCRYPTED_CONTENT_TYPE)

    thumbnail_key = None
    if thumb_ct:
        thumbnail_key = make_encrypted_thumbnail_key(image_key)
        backend.put(thumbnail_key, thumb_ct, ENCRYPTED_CONTENT_TYPE)

    draft.image_key = image_key
    draft.thumbnail_key = thumbnail_key
    draft.encrypted_meta_blob = encrypted_meta_blob
    draft.meta_iv = meta_iv
    draft.file_hash = hashlib.sha256(image_ct).hexdigest()  # cipher hash
    draft.file_hash_plain = file_hash_plain
    draft.file_size = size

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
                    "ai draft finalize rollback: failed to delete %s: %s", k, e,
                )
        raise QuotaExceededError(
            "並行アップロードにより容量上限を超えました。再試行してください。"
        )

    db.session.commit()
    maybe_send_quota_warning(user)
    return draft
