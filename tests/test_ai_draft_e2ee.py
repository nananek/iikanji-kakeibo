"""E5 (#111) AI 下書き画像の E2EE 化 — モデル / スキーマ / 2 段階 upload のテスト。

PR-1 (059 マイグレ + AIDraft モデル E2EE 列) と
PR-2 (サーバ 2 段階 upload: POST /ai/uploads/init + PUT /ai/uploads/<id>、
暗号文配信、create_voucher_from_draft の E2EE 引き継ぎ) のカバレッジ。
後続 PR (クライアント暗号化 / 平文経路撤去) で拡充する。
"""

import hashlib
import io
from base64 import b64encode

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.ai_draft import AIDraft
from app.models.voucher import Voucher
from app.services.storage import (
    get_storage_backend,
    make_ai_draft_encrypted_key,
    make_encrypted_thumbnail_key,
)
from app.services.voucher import create_voucher_from_draft


# iv(12B) || ciphertext || GCM tag(16B) を模した opaque blob (voucher テストと同形式)。
_IMAGE_CT = bytes(12) + b"encrypted-image-payload" + bytes(16)
_THUMB_CT = bytes(12) + b"encrypted-thumb" + bytes(16)
_META_BLOB = b"encrypted-meta-json-blob"
_META_IV = bytes(range(12))
_FILE_HASH_PLAIN = "a" * 64


@pytest.fixture(autouse=True)
def _reset_limiter():
    """各テスト前に limiter をリセット (フルスイートでの RATELIMIT leak 対策)。

    本ファイルは同一 user で init を多数呼ぶため、10/min 上限に累積で達して
    429 になり得る ([[feedback_test_limiter_leak]])。"""
    from app.extensions import limiter
    try:
        limiter.reset()
    except Exception:
        pass
    yield


def _b64(b: bytes) -> str:
    return b64encode(b).decode()


def _init(client, comment=None):
    body = {}
    if comment is not None:
        body["comment"] = comment
    return client.post("/api/v1/ai/uploads/init", json=body)


def _put(client, draft_id, *, image_ct=_IMAGE_CT, thumb_ct=_THUMB_CT,
         meta_blob=_META_BLOB, meta_iv=_META_IV,
         file_hash_plain=_FILE_HASH_PLAIN, include_image=True):
    data = {
        "meta_blob": _b64(meta_blob),
        "meta_iv": _b64(meta_iv),
        "file_hash_plain": file_hash_plain,
    }
    if include_image:
        data["image_ct"] = (io.BytesIO(image_ct), "blob.bin",
                            "application/octet-stream")
    if thumb_ct is not None:
        data["thumb_ct"] = (io.BytesIO(thumb_ct), "thumb.bin",
                            "application/octet-stream")
    return client.put(
        f"/api/v1/ai/uploads/{draft_id}",
        data=data,
        content_type="multipart/form-data",
    )


def _make_draft(user_id, *, aad_id=None, **kwargs):
    defaults = dict(
        user_id=user_id,
        image_key="ai/1/x.bin",
        image_mime="application/octet-stream",
        status="pending",
        suggestions_json="[]",
    )
    defaults.update(kwargs)
    return AIDraft(aad_id=aad_id, **defaults)


class TestAIDraftE2EEColumns:
    def test_e2ee_columns_persist(self, db, user):
        # E2EE 列 (encrypted_meta_blob / meta_iv / file_hash_plain /
        # thumbnail_key / aad_id) が永続化される。
        draft = _make_draft(
            user.id,
            aad_id=123456789,
            encrypted_meta_blob=b"\x01\x02\x03",
            meta_iv=b"\x00" * 12,
            file_hash_plain="a" * 64,
            thumbnail_key="ai/1/x_thumb.bin",
        )
        db.session.add(draft)
        db.session.commit()

        fetched = db.session.get(AIDraft, draft.id)
        assert fetched.aad_id == 123456789
        assert fetched.encrypted_meta_blob == b"\x01\x02\x03"
        assert fetched.meta_iv == b"\x00" * 12
        assert fetched.file_hash_plain == "a" * 64
        assert fetched.thumbnail_key == "ai/1/x_thumb.bin"

    def test_aad_id_unique_per_user(self, db, user):
        # 同一 user で同じ aad_id は UNIQUE(user_id, aad_id) で弾かれる
        # (下書き間 ciphertext swap の検知能力)。
        db.session.add(_make_draft(user.id, aad_id=42))
        db.session.commit()

        db.session.add(_make_draft(user.id, aad_id=42))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_null_aad_id_rows_coexist(self, db, user):
        # レガシー平文下書き (aad_id NULL) は複数併存できる
        # (NULL は UNIQUE 上 distinct 扱い)。
        db.session.add(_make_draft(user.id, aad_id=None))
        db.session.add(_make_draft(user.id, aad_id=None))
        db.session.commit()  # 例外が出ないこと

        assert AIDraft.query.filter_by(
            user_id=user.id, aad_id=None
        ).count() == 2


class TestAIDraftInit:
    """POST /api/v1/ai/uploads/init — 空 AIDraft 採番 + aad_id 生成。"""

    def test_init_creates_empty_pending_draft(self, logged_in_client, user):
        resp = _init(logged_in_client, comment="メモ")
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["ok"] is True
        draft = db.session.get(AIDraft, body["draft_id"])
        assert draft is not None
        assert draft.user_id == user.id
        assert draft.image_key == ""
        assert draft.encrypted_meta_blob is None
        assert draft.status == "pending"
        assert draft.comment == "メモ"
        # aad_id は 63bit、文字列で返り DB 値と一致 (JS BigInt 対策)。
        assert draft.aad_id is not None
        assert 0 < draft.aad_id < 2**63
        assert body["aad_id"] == str(draft.aad_id)

    def test_init_generates_distinct_aad_ids(self, logged_in_client, user):
        ids = {
            _init(logged_in_client).get_json()["aad_id"]
            for _ in range(5)
        }
        assert len(ids) == 5


class TestAIDraftUpload:
    """PUT /api/v1/ai/uploads/<id> — 暗号文 ingest。"""

    def _init_id(self, client):
        return _init(client).get_json()["draft_id"]

    def test_put_stores_ciphertext(self, logged_in_client, user):
        draft_id = self._init_id(logged_in_client)
        resp = _put(logged_in_client, draft_id)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        # file_hash_cipher = SHA-256(image_ct) をサーバが計算。
        assert body["file_hash_cipher"] == hashlib.sha256(_IMAGE_CT).hexdigest()

        draft = db.session.get(AIDraft, draft_id)
        assert draft.encrypted_meta_blob == _META_BLOB
        assert draft.meta_iv == _META_IV
        assert draft.file_hash_plain == _FILE_HASH_PLAIN
        assert draft.file_hash == hashlib.sha256(_IMAGE_CT).hexdigest()
        assert draft.file_size == len(_IMAGE_CT) + len(_THUMB_CT)
        # ストレージキーは d{id} 命名 (voucher 数値キーとの衝突回避)。
        expected_key = make_ai_draft_encrypted_key(user.id, draft_id)
        assert draft.image_key == expected_key
        assert draft.thumbnail_key == make_encrypted_thumbnail_key(expected_key)
        # status は pending のまま (suggestions 保存で analyzed に昇格)。
        assert draft.status == "pending"

        backend = get_storage_backend()
        assert backend.get(draft.image_key) == _IMAGE_CT
        assert backend.get(draft.thumbnail_key) == _THUMB_CT

    def test_put_without_thumb(self, logged_in_client, user):
        draft_id = self._init_id(logged_in_client)
        resp = _put(logged_in_client, draft_id, thumb_ct=None)
        assert resp.status_code == 200
        draft = db.session.get(AIDraft, draft_id)
        assert draft.thumbnail_key is None
        assert draft.file_size == len(_IMAGE_CT)

    def test_put_unknown_draft_404(self, logged_in_client, user):
        resp = _put(logged_in_client, 999999)
        assert resp.status_code == 404

    def test_put_overwrite_409(self, logged_in_client, user):
        draft_id = self._init_id(logged_in_client)
        assert _put(logged_in_client, draft_id).status_code == 200
        # 2 回目は上書き禁止 (電帳法)。
        resp = _put(logged_in_client, draft_id)
        assert resp.status_code == 409

    def test_put_missing_image_400(self, logged_in_client, user):
        draft_id = self._init_id(logged_in_client)
        resp = _put(logged_in_client, draft_id, include_image=False)
        assert resp.status_code == 400

    def test_put_bad_file_hash_plain_400(self, logged_in_client, user):
        draft_id = self._init_id(logged_in_client)
        resp = _put(logged_in_client, draft_id, file_hash_plain="xyz")
        assert resp.status_code == 400

    def test_put_image_too_small_400(self, logged_in_client, user):
        # iv+tag 未満 (GCM 最小バイト数を下回る) は弾く。
        draft_id = self._init_id(logged_in_client)
        resp = _put(logged_in_client, draft_id, image_ct=b"short")
        assert resp.status_code == 400

    def test_put_other_user_404(self, logged_in_client, user, second_user):
        # second_user の下書きを user として PUT → 404 (IDOR 防止)。
        other = AIDraft(
            user_id=second_user.id, image_key="",
            image_mime="application/octet-stream",
            status="pending", suggestions_json="[]", aad_id=777,
        )
        db.session.add(other)
        db.session.commit()
        resp = _put(logged_in_client, other.id)
        assert resp.status_code == 404


class TestAIDraftServing:
    """暗号化下書き画像の octet-stream 配信 (ai_journal.draft_image)。"""

    def test_encrypted_draft_served_as_octet_stream(self, logged_in_client, user):
        draft_id = _init(logged_in_client).get_json()["draft_id"]
        _put(logged_in_client, draft_id)
        resp = logged_in_client.get(f"/ai-journal/drafts/{draft_id}/image")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        assert resp.data == _IMAGE_CT

    def test_encrypted_draft_thumb_served(self, logged_in_client, user):
        draft_id = _init(logged_in_client).get_json()["draft_id"]
        _put(logged_in_client, draft_id)
        resp = logged_in_client.get(
            f"/ai-journal/drafts/{draft_id}/image?size=thumb"
        )
        assert resp.status_code == 200
        assert resp.data == _THUMB_CT


class TestCreateVoucherFromDraftE2EE:
    """create_voucher_from_draft が E2EE 成果物 + aad_id を引き継ぐ。"""

    def test_carries_e2ee_fields_and_aad_id(self, db, user):
        draft = AIDraft(
            user_id=user.id, image_key="vouchers/1/d5.bin",
            image_mime="application/octet-stream",
            status="analyzed", suggestions_json="[]",
            encrypted_meta_blob=b"meta", meta_iv=bytes(12),
            file_hash="c" * 64, file_hash_plain="d" * 64,
            thumbnail_key="vouchers/1/d5_thumb.bin", aad_id=555,
            file_size=123,
        )
        db.session.add(draft)
        db.session.commit()

        voucher = create_voucher_from_draft(draft, journal_entry_id=None)
        db.session.commit()

        assert voucher.image_key == "vouchers/1/d5.bin"
        assert voucher.encrypted_meta_blob == b"meta"
        assert voucher.meta_iv == bytes(12)
        assert voucher.file_hash_plain == "d" * 64
        assert voucher.thumbnail_key == "vouchers/1/d5_thumb.bin"
        # aad_id 引き継ぎ — 再暗号化なしで AAD を維持する肝。
        assert voucher.aad_id == 555
        # 下書きは削除される。
        assert db.session.get(AIDraft, draft.id) is None

    def test_legacy_plaintext_draft_still_works(self, db, user):
        # E2EE 列が全て None の平文下書きは従来通り平文証憑になる (両対応)。
        draft = AIDraft(
            user_id=user.id, image_key="vouchers/1/9.png",
            image_mime="image/png", status="analyzed",
            suggestions_json="[]", file_hash="e" * 64, file_size=50,
        )
        db.session.add(draft)
        db.session.commit()

        voucher = create_voucher_from_draft(draft, journal_entry_id=None)
        db.session.commit()

        # E5 PR-5 (#111): voucher.image_mime 列は DROP 済。平文下書きは E2EE 列が
        # 全て None の平文証憑になる (image_key は引き継ぐ)。
        assert voucher.image_key == "vouchers/1/9.png"
        assert voucher.encrypted_meta_blob is None
        assert voucher.aad_id is None


class TestAIDraftThumbnailOrphan:
    """E5 (#111): E2EE 下書き削除で暗号文サムネ (_thumb.bin) を孤立させない。

    PR-3 で E2EE 下書きはクライアント生成サムネ (thumbnail_key, _thumb.bin) を
    持つようになったが、削除経路は従来 _thumb.jpg (make_thumbnail_key) のみ消して
    いた。全経路で thumbnail_key も消すことを検証 (PR-H の voucher 修正と同型)。
    """

    def _make_e2ee_draft(self, client):
        draft_id = _init(client).get_json()["draft_id"]
        _put(client, draft_id)  # 画像 + _thumb.bin を保存
        return draft_id

    def test_drafts_delete_removes_encrypted_thumbnail(self, logged_in_client, user):
        draft_id = self._make_e2ee_draft(logged_in_client)
        draft = db.session.get(AIDraft, draft_id)
        image_key, thumb_key = draft.image_key, draft.thumbnail_key
        backend = get_storage_backend()
        assert backend.exists(thumb_key)
        assert thumb_key.endswith("_thumb.bin")

        resp = logged_in_client.post(f"/ai-journal/drafts/{draft_id}/delete")
        assert resp.status_code in (200, 302)
        # 暗号文サムネ・本体とも孤立せず削除される。
        assert not backend.exists(thumb_key)
        assert not backend.exists(image_key)

    def test_account_deletion_removes_encrypted_thumbnail(self, db, logged_in_client, user):
        from app.services.account_deletion import delete_user_account
        draft_id = self._make_e2ee_draft(logged_in_client)
        draft = db.session.get(AIDraft, draft_id)
        thumb_key = draft.thumbnail_key
        backend = get_storage_backend()
        assert backend.exists(thumb_key)

        delete_user_account(user.id)
        assert not backend.exists(thumb_key)

    def test_restore_delete_removes_encrypted_thumbnail(self, db, logged_in_client, user):
        # PR-4 レビュー指摘: restore 時の破壊的削除経路も _thumb.bin を消すこと。
        from app.services.backup_restore import _delete_user_data_for_restore
        draft_id = self._make_e2ee_draft(logged_in_client)
        draft = db.session.get(AIDraft, draft_id)
        thumb_key = draft.thumbnail_key
        backend = get_storage_backend()
        assert backend.exists(thumb_key)

        _delete_user_data_for_restore(user.id, backend)
        db.session.commit()
        assert not backend.exists(thumb_key)

    def test_upload_cleanup_removes_temp_encrypted_thumbnail(self, db, logged_in_client, user):
        # PR-4 レビュー指摘: upload 画面の temp 下書きクリーンアップ経路。
        draft_id = self._make_e2ee_draft(logged_in_client)
        draft = db.session.get(AIDraft, draft_id)
        draft.status = "temp"  # upload() が消す対象
        db.session.commit()
        thumb_key = draft.thumbnail_key
        backend = get_storage_backend()
        assert backend.exists(thumb_key)

        # upload 画面 GET で temp 下書きをクリーンアップ。
        resp = logged_in_client.get("/ai-journal/")
        assert resp.status_code == 200
        assert not backend.exists(thumb_key)
