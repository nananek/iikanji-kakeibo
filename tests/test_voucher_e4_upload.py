"""E4 (#111) PR-B: 証憑画像 2 段階 E2EE upload のテスト。

POST /api/v1/vouchers/init → PUT /api/v1/vouchers/<id> のフロー、暗号文 ingest、
file_hash_cipher サーバ計算、上書き禁止、代理閲覧ブロック、暗号文配信を検証する。
"""

import hashlib
import io
from base64 import b64encode

import pytest

from app.extensions import db as _db
from app.models.storage import StorageUsage
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.services.storage import get_storage_backend
from tests.conftest import make_journal


# iv(12B) || ciphertext || GCM tag(16B) を模した opaque blob。
_IMAGE_CT = bytes(12) + b"encrypted-image-payload" + bytes(16)
_THUMB_CT = bytes(12) + b"encrypted-thumb" + bytes(16)
_META_BLOB = b"encrypted-meta-json-blob"
_META_IV = bytes(range(12))
_FILE_HASH_PLAIN = "a" * 64


def _b64(b: bytes) -> str:
    return b64encode(b).decode()


def _init_voucher(client, journal_entry_id=None):
    body = {}
    if journal_entry_id is not None:
        body["journal_entry_id"] = journal_entry_id
    resp = client.post("/api/v1/vouchers/init", json=body)
    return resp


def _upload(client, voucher_id, *, image_ct=_IMAGE_CT, thumb_ct=_THUMB_CT,
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
        f"/api/v1/vouchers/{voucher_id}",
        data=data,
        content_type="multipart/form-data",
    )


class TestVoucherInit:
    def test_init_creates_empty_voucher(self, logged_in_client, user):
        resp = _init_voucher(logged_in_client)
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["ok"] is True
        vid = body["voucher_id"]
        v = _db.session.get(Voucher, vid)
        assert v is not None
        assert v.user_id == user.id
        assert v.image_key == ""
        assert v.encrypted_meta_blob is None

    def test_init_links_journal_entry(self, logged_in_client, user, accounts, db):
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        resp = _init_voucher(logged_in_client, journal_entry_id=entry.id)
        assert resp.status_code == 201
        v = _db.session.get(Voucher, resp.get_json()["voucher_id"])
        assert v.journal_entry_id == entry.id

    def test_init_other_user_entry_404(
        self, logged_in_client, second_user, second_user_accounts, db
    ):
        entry = make_journal(db, second_user.id, "5010", "1010", 1000)
        resp = _init_voucher(logged_in_client, journal_entry_id=entry.id)
        assert resp.status_code == 404

    def test_init_invalid_entry_id_400(self, logged_in_client, user):
        resp = logged_in_client.post(
            "/api/v1/vouchers/init", json={"journal_entry_id": "abc"},
        )
        assert resp.status_code == 400

    def test_init_requires_auth(self, client):
        resp = client.post("/api/v1/vouchers/init", json={})
        assert resp.status_code == 401


class TestVoucherUpload:
    def test_upload_success(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        resp = _upload(logged_in_client, vid)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["file_hash_cipher"] == hashlib.sha256(_IMAGE_CT).hexdigest()

        v = _db.session.get(Voucher, vid)
        assert v.image_key.endswith(".bin")
        assert v.thumbnail_key and v.thumbnail_key.endswith("_thumb.bin")
        assert v.encrypted_meta_blob == _META_BLOB
        assert v.meta_iv == _META_IV
        assert v.file_hash_plain == _FILE_HASH_PLAIN
        assert v.file_hash == hashlib.sha256(_IMAGE_CT).hexdigest()
        assert v.file_size == len(_IMAGE_CT) + len(_THUMB_CT)

        # ストレージに暗号文がそのまま保存されている
        backend = get_storage_backend()
        assert backend.get(v.image_key) == _IMAGE_CT
        assert backend.get(v.thumbnail_key) == _THUMB_CT

    def test_upload_creates_attached_audit_log(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        _upload(logged_in_client, vid)
        log = VoucherAuditLog.query.filter_by(
            voucher_id=vid, action="attached"
        ).first()
        assert log is not None

    def test_upload_without_thumb_ok(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        resp = _upload(logged_in_client, vid, thumb_ct=None)
        assert resp.status_code == 200
        v = _db.session.get(Voucher, vid)
        assert v.thumbnail_key is None
        assert v.file_size == len(_IMAGE_CT)

    def test_overwrite_rejected_409(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        assert _upload(logged_in_client, vid).status_code == 200
        # 2 回目は上書き禁止 (電帳法)
        assert _upload(logged_in_client, vid).status_code == 409

    def test_missing_image_ct_400(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        resp = _upload(logged_in_client, vid, include_image=False)
        assert resp.status_code == 400

    def test_image_ct_too_small_400(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        resp = _upload(logged_in_client, vid, image_ct=b"short")  # < iv+tag
        assert resp.status_code == 400

    def test_bad_meta_iv_length_400(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        resp = _upload(logged_in_client, vid, meta_iv=bytes(8))  # 12B でない
        assert resp.status_code == 400

    def test_missing_meta_400(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        data = {
            "file_hash_plain": _FILE_HASH_PLAIN,
            "image_ct": (io.BytesIO(_IMAGE_CT), "blob.bin",
                         "application/octet-stream"),
        }
        resp = logged_in_client.put(
            f"/api/v1/vouchers/{vid}", data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_bad_file_hash_plain_400(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        resp = _upload(logged_in_client, vid, file_hash_plain="xyz")
        assert resp.status_code == 400

    def test_upload_other_user_voucher_404(
        self, logged_in_client, second_user, db
    ):
        # second_user の init voucher を user が PUT しようとする
        v = Voucher(user_id=second_user.id, image_key="",
                    image_mime="application/octet-stream")
        db.session.add(v)
        db.session.commit()
        resp = _upload(logged_in_client, v.id)
        assert resp.status_code == 404

    def test_upload_quota_413(self, logged_in_client, user, db, app, monkeypatch):
        monkeypatch.setitem(app.config, "STORAGE_QUOTA_BYTES_DEFAULT", 10)
        db.session.add(StorageUsage(user_id=user.id, used_bytes=5))
        db.session.commit()
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        resp = _upload(logged_in_client, vid)
        assert resp.status_code == 413


class TestProxyBlocked:
    """代理閲覧 (acting_as) 中は init / PUT とも 403。"""

    def _grant_lv3(self, db, owner, auditor):
        from app.models.audit import AuditGrant
        grant = AuditGrant(
            owner_user_id=owner.id, auditor_user_id=auditor.id,
            permission_level=3, status="active",
        )
        db.session.add(grant)
        db.session.commit()

    def test_init_blocked_during_proxy(
        self, client, db, user, auditor
    ):
        self._grant_lv3(db, user, auditor)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 3
        resp = client.post("/api/v1/vouchers/init", json={})
        assert resp.status_code == 403
        assert Voucher.query.count() == 0

    def test_upload_blocked_during_proxy(
        self, client, db, user, auditor
    ):
        # 本人として init → その後 auditor が代理で PUT を試みる
        v = Voucher(user_id=user.id, image_key="",
                    image_mime="application/octet-stream")
        db.session.add(v)
        db.session.commit()
        self._grant_lv3(db, user, auditor)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 3
        resp = _upload(client, v.id)
        assert resp.status_code == 403


class TestAtomicClaim:
    """PR-B レビュー ①: finalize_voucher_upload の原子的クレーム。"""

    def test_finalize_conflict_when_already_claimed(self, app, db, user):
        """DB 上で image_key が既に確定済みの行を finalize すると
        VoucherUploadConflict を送出する (並行 PUT の敗者)。"""
        from app.services.voucher import (
            VoucherUploadConflict, finalize_voucher_upload,
        )

        v = Voucher(
            user_id=user.id,
            image_key="vouchers/x/already.bin",  # 既に確定済み (勝者が claim 済)
            image_mime="application/octet-stream",
        )
        db.session.add(v)
        db.session.commit()

        with app.app_context():
            with pytest.raises(VoucherUploadConflict):
                finalize_voucher_upload(
                    v, _IMAGE_CT, _THUMB_CT, _META_BLOB, _META_IV,
                    _FILE_HASH_PLAIN,
                )


class TestServeEncryptedVoucher:
    def test_serves_ciphertext_octet_stream(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        _upload(logged_in_client, vid)
        resp = logged_in_client.get(f"/ai-journal/voucher/{vid}/image")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        assert resp.data == _IMAGE_CT

    def test_serves_thumbnail(self, logged_in_client, user):
        vid = _init_voucher(logged_in_client).get_json()["voucher_id"]
        _upload(logged_in_client, vid)
        resp = logged_in_client.get(
            f"/ai-journal/voucher/{vid}/image?size=thumb"
        )
        assert resp.status_code == 200
        assert resp.data == _THUMB_CT
