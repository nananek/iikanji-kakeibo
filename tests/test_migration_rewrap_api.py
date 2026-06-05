"""E7 (#114): temp-MK 再ラップ移行 API のテスト。

GET  /api/v1/migration/temp-mk             — temp-MK 配布 (owner セッション限定)
POST /api/v1/migration/rewrap              — blob/iv の in-place 差し替え
PUT  /api/v1/migration/rewrap-image        — 証憑画像の暗号文上書き
POST /api/v1/migration/finalize            — temp-MK 破棄
GET  /api/v1/migration/voucher-blobs       — 証憑メタ/監査ログ blob 一括読み取り
GET  /api/v1/migration/voucher-image/<id>  — 証憑画像/サムネ配信 (論理削除込み)
"""

import hashlib
from base64 import b64decode, b64encode
from datetime import datetime, timezone

import pytest

from app.extensions import db as _db
from app.models.balance_cache import BalanceCacheBlob
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.medical import MedicalExpense
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from tests.conftest import _auth_header, make_journal, make_voucher


def _b64(data: bytes) -> str:
    return b64encode(data).decode("ascii")


# 再ラップ後を模した新しい blob/iv (実値はテストでは任意のダミー)。
NEW_BLOB = bytes([0x99]) * 48
NEW_IV = bytes([0x88]) * 12


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return client


@pytest.fixture
def migrating_user(db, user):
    """temp-MK・公開鍵が設定された移行待ちユーザー。"""
    user.public_key = b"x" * 32
    user.migration_temp_mk = bytes(range(32))
    db.session.commit()
    return user


# ─────────────────────────── GET /migration/temp-mk ───────────────────────────


class TestTempMk:
    def test_returns_temp_mk_for_owner_session(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.get("/api/v1/migration/temp-mk")
        assert r.status_code == 200
        body = r.get_json()
        assert body["active"] is True
        assert b64decode(body["temp_mk"]) == bytes(range(32))

    def test_inactive_when_temp_mk_cleared(self, db, client, user):
        user.public_key = b"x" * 32
        user.migration_temp_mk = None
        db.session.commit()
        _login(client, user)
        r = client.get("/api/v1/migration/temp-mk")
        assert r.status_code == 200
        body = r.get_json()
        assert body["active"] is False
        assert body["temp_mk"] is None

    def test_409_when_no_public_key(self, db, client, user):
        user.public_key = None
        user.migration_temp_mk = bytes(range(32))
        db.session.commit()
        _login(client, user)
        r = client.get("/api/v1/migration/temp-mk")
        assert r.status_code == 409

    def test_bearer_token_rejected(self, db, client, migrating_user, api_key_raw):
        # セッションではなく Bearer トークン → temp-MK は渡さない (露出最小化)。
        raw_key, _ = api_key_raw
        r = client.get("/api/v1/migration/temp-mk", headers=_auth_header(raw_key))
        assert r.status_code == 403

    def test_unauthenticated_401(self, db, client):
        r = client.get("/api/v1/migration/temp-mk")
        assert r.status_code == 401

    def test_auditor_rejected(self, db, client, auditor):
        auditor.public_key = b"x" * 32
        auditor.migration_temp_mk = bytes(range(32))
        db.session.commit()
        _login(client, auditor)
        r = client.get("/api/v1/migration/temp-mk")
        assert r.status_code == 403

    def test_inactive_user_blocked_at_session_load(self, db, client, migrating_user):
        # §16.5 ロック: is_active=False は user_loader 段階で弾かれ、セッション
        # 認証自体が成立しない (401)。temp-MK は当然渡らない。
        migrating_user.is_active = False
        db.session.commit()
        _login(client, migrating_user)
        r = client.get("/api/v1/migration/temp-mk")
        assert r.status_code == 401


# ─────────────────────────── POST /migration/rewrap ───────────────────────────


class TestRewrap:
    def test_rewrap_journal_entry(self, db, client, migrating_user):
        entry = make_journal(db, migrating_user.id, "1010", "5010", 1000)
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je",
            "items": [{
                "id": entry.id,
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "updated": 1, "skipped": 0}
        refreshed = _db.session.get(JournalEntry, entry.id)
        assert refreshed.encrypted_blob == NEW_BLOB
        assert refreshed.blob_iv == NEW_IV

    def test_rewrap_journal_line_by_account_user_id(self, db, client, migrating_user):
        entry = make_journal(db, migrating_user.id, "1010", "5010", 1000)
        line = entry.lines[0]
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "jel",
            "items": [{
                "id": line.id,
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 200
        assert r.get_json()["updated"] == 1
        assert _db.session.get(JournalEntryLine, line.id).encrypted_blob == NEW_BLOB

    def test_rewrap_medical_expense(self, db, client, migrating_user):
        me = MedicalExpense(
            user_id=migrating_user.id,
            encrypted_blob=b"old", blob_iv=b"oldiv",
        )
        db.session.add(me)
        db.session.commit()
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "me",
            "items": [{
                "id": me.id,
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 200
        assert _db.session.get(MedicalExpense, me.id).blob_iv == NEW_IV

    def test_rewrap_voucher_meta(self, db, client, migrating_user):
        v = make_voucher(db, migrating_user.id)
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "vmeta",
            "items": [{
                "id": v.id,
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 200
        refreshed = _db.session.get(Voucher, v.id)
        assert refreshed.encrypted_meta_blob == NEW_BLOB
        assert refreshed.meta_iv == NEW_IV

    def test_rewrap_voucher_audit_log(self, db, client, migrating_user):
        v = make_voucher(db, migrating_user.id)
        log = VoucherAuditLog(
            voucher_id=v.id, user_id=migrating_user.id, action="attached",
            encrypted_detail_blob=b"old", detail_iv=b"oldiv",
        )
        db.session.add(log)
        db.session.commit()
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "valog",
            "items": [{
                "id": log.id,
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 200
        refreshed = _db.session.get(VoucherAuditLog, log.id)
        assert refreshed.encrypted_detail_blob == NEW_BLOB
        assert refreshed.detail_iv == NEW_IV

    def test_rewrap_balance_cache_blob_by_year_period(self, db, client, migrating_user):
        bcb = BalanceCacheBlob(
            user_id=migrating_user.id, year=2025, period=3,
            encrypted_blob=b"old", blob_iv=bytes(12),
        )
        db.session.add(bcb)
        db.session.commit()
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "bcb",
            "items": [{
                "year": 2025, "period": 3,
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 200
        assert r.get_json()["updated"] == 1
        refreshed = _db.session.get(BalanceCacheBlob, bcb.id)
        assert refreshed.encrypted_blob == NEW_BLOB

    def test_nonexistent_id_is_skipped(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je",
            "items": [{
                "id": 999999,
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "updated": 0, "skipped": 1}

    def test_idor_other_users_row_not_updated(
        self, db, client, migrating_user, second_user,
    ):
        # second_user の仕訳は migrating_user の rewrap で更新されない。
        other = make_journal(db, second_user.id, "1010", "5010", 1000)
        original_blob = _db.session.get(JournalEntry, other.id).encrypted_blob
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je",
            "items": [{
                "id": other.id,
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 200
        assert r.get_json()["skipped"] == 1
        assert _db.session.get(JournalEntry, other.id).encrypted_blob == original_blob

    def test_idor_bcb_other_users_row_not_updated(
        self, db, client, migrating_user, second_user,
    ):
        # bcb は (year, period) 複合キーで所有者スコープする別経路。他ユーザーの
        # year/period を指定しても user_id フィルタで skip され更新されない。
        bcb = BalanceCacheBlob(
            user_id=second_user.id, year=2025, period=3,
            encrypted_blob=b"other", blob_iv=bytes(12),
        )
        db.session.add(bcb)
        db.session.commit()
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "bcb",
            "items": [{
                "year": 2025, "period": 3,
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 200
        assert r.get_json()["skipped"] == 1
        assert _db.session.get(BalanceCacheBlob, bcb.id).encrypted_blob == b"other"

    def test_invalid_table_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "users", "items": [],
        })
        assert r.status_code == 400

    def test_invalid_base64_400(self, db, client, migrating_user):
        entry = make_journal(db, migrating_user.id, "1010", "5010", 1000)
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je",
            "items": [{
                "id": entry.id,
                "encrypted_blob": "!!!not-base64!!!",
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 400

    def test_missing_blob_400(self, db, client, migrating_user):
        entry = make_journal(db, migrating_user.id, "1010", "5010", 1000)
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je",
            "items": [{"id": entry.id}],
        })
        assert r.status_code == 400

    def test_too_many_items_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        items = [
            {"id": i, "encrypted_blob": _b64(NEW_BLOB), "blob_iv": _b64(NEW_IV)}
            for i in range(501)
        ]
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je", "items": items,
        })
        assert r.status_code == 400

    def test_auditor_rejected(self, db, client, auditor):
        _login(client, auditor)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je", "items": [],
        })
        assert r.status_code == 403

    def test_no_json_body_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap")
        assert r.status_code == 400

    def test_items_not_list_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je", "items": "nope",
        })
        assert r.status_code == 400

    def test_item_not_dict_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je", "items": ["nope"],
        })
        assert r.status_code == 400

    def test_id_not_int_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je",
            "items": [{
                "id": "abc",
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 400

    def test_bcb_missing_year_period_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "bcb",
            "items": [{
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 400

    def test_bearer_token_rejected(self, db, client, migrating_user, api_key_raw):
        # 書き込み系もセッション限定。write 権限のある Bearer トークンでも 403。
        raw_key, _ = api_key_raw
        r = client.post(
            "/api/v1/migration/rewrap",
            json={"table": "je", "items": []},
            headers=_auth_header(raw_key),
        )
        assert r.status_code == 403

    def test_commit_error_returns_500(self, db, client, migrating_user, monkeypatch):
        entry = make_journal(db, migrating_user.id, "1010", "5010", 1000)
        _login(client, migrating_user)

        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(_db.session, "commit", boom)
        r = client.post("/api/v1/migration/rewrap", json={
            "table": "je",
            "items": [{
                "id": entry.id,
                "encrypted_blob": _b64(NEW_BLOB),
                "blob_iv": _b64(NEW_IV),
            }],
        })
        assert r.status_code == 500


# ─────────────────────────── PUT /migration/rewrap-image ──────────────────────


class TestRewrapImage:
    def _seed_image(self, db, user_id, image_ct=b"\x01" * 48):
        from app.services.storage import get_storage_backend
        v = make_voucher(db, user_id, image_key=f"vouchers/{user_id}/img.bin")
        get_storage_backend().put(v.image_key, image_ct, "application/octet-stream")
        v.file_hash = hashlib.sha256(image_ct).hexdigest()
        db.session.commit()
        return v

    def test_overwrites_image_and_recomputes_hash(self, db, client, migrating_user):
        from app.services.storage import get_storage_backend
        v = self._seed_image(db, migrating_user.id)
        new_ct = b"\x02" * 64
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": v.id,
            "image_ct": _b64(new_ct),
        })
        assert r.status_code == 200
        assert get_storage_backend().get(v.image_key) == new_ct
        refreshed = _db.session.get(Voucher, v.id)
        assert refreshed.file_hash == hashlib.sha256(new_ct).hexdigest()

    def test_overwrites_thumbnail(self, db, client, migrating_user):
        from app.services.storage import (
            get_storage_backend, make_encrypted_thumbnail_key,
        )
        v = self._seed_image(db, migrating_user.id)
        new_ct = b"\x02" * 64
        new_thumb = b"\x03" * 40
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": v.id,
            "image_ct": _b64(new_ct),
            "thumb_ct": _b64(new_thumb),
        })
        assert r.status_code == 200
        thumb_key = _db.session.get(Voucher, v.id).thumbnail_key
        assert thumb_key == make_encrypted_thumbnail_key(v.image_key)
        assert get_storage_backend().get(thumb_key) == new_thumb

    def test_404_for_missing_voucher(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": 999999,
            "image_ct": _b64(b"\x02" * 48),
        })
        assert r.status_code == 404

    def test_idor_other_users_voucher_404(
        self, db, client, migrating_user, second_user,
    ):
        v = self._seed_image(db, second_user.id)
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": v.id,
            "image_ct": _b64(b"\x02" * 48),
        })
        assert r.status_code == 404

    def test_short_image_ct_400(self, db, client, migrating_user):
        v = self._seed_image(db, migrating_user.id)
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": v.id,
            "image_ct": _b64(b"\x02" * 10),  # < 12+16
        })
        assert r.status_code == 400

    def test_no_json_body_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image")
        assert r.status_code == 400

    def test_voucher_id_not_int_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": "x", "image_ct": _b64(b"\x02" * 48),
        })
        assert r.status_code == 400

    def test_missing_image_ct_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": 1,
        })
        assert r.status_code == 400

    def test_invalid_image_base64_400(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": 1, "image_ct": "!!!bad!!!",
        })
        assert r.status_code == 400

    def test_invalid_thumb_base64_400(self, db, client, migrating_user):
        v = self._seed_image(db, migrating_user.id)
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": v.id,
            "image_ct": _b64(b"\x02" * 48),
            "thumb_ct": "!!!bad!!!",
        })
        assert r.status_code == 400

    def test_short_thumb_ct_400(self, db, client, migrating_user):
        v = self._seed_image(db, migrating_user.id)
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": v.id,
            "image_ct": _b64(b"\x02" * 48),
            "thumb_ct": _b64(b"\x03" * 10),
        })
        assert r.status_code == 400

    def test_non_str_thumb_400(self, db, client, migrating_user):
        v = self._seed_image(db, migrating_user.id)
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": v.id,
            "image_ct": _b64(b"\x02" * 48),
            "thumb_ct": 123,
        })
        assert r.status_code == 400

    def test_storage_error_returns_500(self, db, client, migrating_user, monkeypatch):
        import app.views.api as api_mod
        v = self._seed_image(db, migrating_user.id)
        _login(client, migrating_user)

        class _BoomBackend:
            def put(self, *a, **k):
                raise RuntimeError("storage down")

        monkeypatch.setattr(api_mod, "get_storage_backend", lambda: _BoomBackend())
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": v.id,
            "image_ct": _b64(b"\x02" * 48),
        })
        assert r.status_code == 500

    def test_oversize_image_ct_400(self, db, client, migrating_user):
        v = self._seed_image(db, migrating_user.id)
        _login(client, migrating_user)
        # 平文 10MB + GCM オーバヘッドの上限を超える
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": v.id,
            "image_ct": _b64(b"\x02" * (10 * 1024 * 1024 + 2048)),
        })
        assert r.status_code == 400

    def test_oversize_thumb_ct_400(self, db, client, migrating_user):
        v = self._seed_image(db, migrating_user.id)
        _login(client, migrating_user)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": v.id,
            "image_ct": _b64(b"\x02" * 48),
            "thumb_ct": _b64(b"\x03" * (512 * 1024 + 2048)),
        })
        assert r.status_code == 400

    def test_bearer_token_rejected(self, db, client, migrating_user, api_key_raw):
        raw_key, _ = api_key_raw
        r = client.put(
            "/api/v1/migration/rewrap-image",
            json={"voucher_id": 1, "image_ct": _b64(b"\x02" * 48)},
            headers=_auth_header(raw_key),
        )
        assert r.status_code == 403

    def test_auditor_rejected(self, db, client, auditor):
        _login(client, auditor)
        r = client.put("/api/v1/migration/rewrap-image", json={
            "voucher_id": 1, "image_ct": _b64(b"\x02" * 48),
        })
        assert r.status_code == 403


# ─────────────────────────── POST /migration/finalize ─────────────────────────


class TestFinalize:
    def test_clears_temp_mk(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.post("/api/v1/migration/finalize")
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "finalized": True}
        from app.models.user import User
        assert _db.session.get(User, migrating_user.id).migration_temp_mk is None

    def test_idempotent_when_already_cleared(self, db, client, user):
        user.migration_temp_mk = None
        db.session.commit()
        _login(client, user)
        r = client.post("/api/v1/migration/finalize")
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "finalized": False}

    def test_commit_error_returns_500(self, db, client, migrating_user, monkeypatch):
        _login(client, migrating_user)

        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(_db.session, "commit", boom)
        r = client.post("/api/v1/migration/finalize")
        assert r.status_code == 500

    def test_bearer_token_rejected(self, db, client, migrating_user, api_key_raw):
        # finalize を Bearer で叩けると temp_mk 誤爆 → データ不整合。403 で遮断。
        raw_key, _ = api_key_raw
        r = client.post(
            "/api/v1/migration/finalize",
            headers=_auth_header(raw_key),
        )
        assert r.status_code == 403
        # temp_mk は消えていない
        from app.models.user import User
        assert _db.session.get(User, migrating_user.id).migration_temp_mk is not None

    def test_auditor_rejected(self, db, client, auditor):
        _login(client, auditor)
        r = client.post("/api/v1/migration/finalize")
        assert r.status_code == 403


# ─────────────────────── GET /migration/voucher-blobs ─────────────────────────


def _seed_voucher_with_meta(db, user_id, aad_id=123, deleted=False,
                            with_log=True):
    v = make_voucher(db, user_id, image_key=f"vouchers/{user_id}/v.bin")
    v.aad_id = aad_id
    v.encrypted_meta_blob = b"metablob"
    v.meta_iv = bytes(12)
    v.thumbnail_key = f"vouchers/{user_id}/v_thumb.bin"
    if deleted:
        v.deleted_at = datetime.now(timezone.utc)
    db.session.add(v)
    db.session.commit()
    if with_log:
        log = VoucherAuditLog(
            voucher_id=v.id, user_id=user_id, action="attached",
            encrypted_detail_blob=b"logblob", detail_iv=bytes(12),
        )
        db.session.add(log)
        db.session.commit()
    return v


class TestVoucherBlobs:
    def test_returns_meta_and_logs(self, db, client, migrating_user):
        v = _seed_voucher_with_meta(db, migrating_user.id, aad_id=777)
        _login(client, migrating_user)
        r = client.get("/api/v1/migration/voucher-blobs")
        assert r.status_code == 200
        body = r.get_json()
        assert body["total"] == 1
        item = body["vouchers"][0]
        assert item["id"] == v.id
        assert item["aad_id"] == "777"
        assert b64decode(item["encrypted_meta_blob"]) == b"metablob"
        assert item["has_image"] is True
        assert item["has_thumbnail"] is True
        assert len(item["logs"]) == 1
        assert b64decode(item["logs"][0]["encrypted_detail_blob"]) == b"logblob"

    def test_includes_soft_deleted(self, db, client, migrating_user):
        _seed_voucher_with_meta(db, migrating_user.id, aad_id=1, deleted=True,
                                with_log=False)
        _login(client, migrating_user)
        r = client.get("/api/v1/migration/voucher-blobs")
        assert r.status_code == 200
        # 論理削除済みも再ラップ対象として返る
        assert r.get_json()["total"] == 1

    def test_idor_only_own_vouchers(self, db, client, migrating_user, second_user):
        _seed_voucher_with_meta(db, second_user.id, aad_id=9, with_log=False)
        _login(client, migrating_user)
        r = client.get("/api/v1/migration/voucher-blobs")
        assert r.status_code == 200
        assert r.get_json()["total"] == 0

    def test_pagination(self, db, client, migrating_user):
        for i in range(3):
            _seed_voucher_with_meta(db, migrating_user.id, aad_id=100 + i,
                                    with_log=False)
        _login(client, migrating_user)
        r = client.get("/api/v1/migration/voucher-blobs?page=1&per_page=2")
        body = r.get_json()
        assert body["total"] == 3
        assert len(body["vouchers"]) == 2
        r2 = client.get("/api/v1/migration/voucher-blobs?page=2&per_page=2")
        assert len(r2.get_json()["vouchers"]) == 1

    def test_invalid_page_params_default(self, db, client, migrating_user):
        _seed_voucher_with_meta(db, migrating_user.id, aad_id=1, with_log=False)
        _login(client, migrating_user)
        r = client.get("/api/v1/migration/voucher-blobs?page=abc&per_page=xyz")
        assert r.status_code == 200
        body = r.get_json()
        assert body["page"] == 1
        assert body["per_page"] == 200

    def test_bearer_rejected(self, db, client, migrating_user, api_key_raw):
        raw_key, _ = api_key_raw
        r = client.get("/api/v1/migration/voucher-blobs",
                       headers=_auth_header(raw_key))
        assert r.status_code == 403

    def test_auditor_rejected(self, db, client, auditor):
        _login(client, auditor)
        r = client.get("/api/v1/migration/voucher-blobs")
        assert r.status_code == 403


# ─────────────────── GET /migration/voucher-image/<id> ────────────────────────


class TestVoucherImage:
    def _seed(self, db, user_id, image_ct=b"\x01" * 48, deleted=False,
              thumb_ct=None):
        from app.services.storage import (
            get_storage_backend, make_encrypted_thumbnail_key,
        )
        v = make_voucher(db, user_id, image_key=f"vouchers/{user_id}/img.bin")
        # 再ラップ対象の実証憑は E2EE 化済み (encrypted_meta_blob あり)。この場合
        # のみ serve_voucher_image が ?size=thumb で thumbnail_key を使う。
        v.encrypted_meta_blob = b"meta"
        v.meta_iv = bytes(12)
        if deleted:
            v.deleted_at = datetime.now(timezone.utc)
        backend = get_storage_backend()
        backend.put(v.image_key, image_ct, "application/octet-stream")
        if thumb_ct is not None:
            v.thumbnail_key = make_encrypted_thumbnail_key(v.image_key)
            backend.put(v.thumbnail_key, thumb_ct, "application/octet-stream")
        db.session.commit()
        return v

    def test_serves_image_bytes(self, db, client, migrating_user):
        ct = b"\x05" * 60
        v = self._seed(db, migrating_user.id, image_ct=ct)
        _login(client, migrating_user)
        r = client.get(f"/api/v1/migration/voucher-image/{v.id}")
        assert r.status_code == 200
        assert r.data == ct

    def test_serves_thumbnail(self, db, client, migrating_user):
        img = b"\x05" * 60
        thumb = b"\x07" * 40
        v = self._seed(db, migrating_user.id, image_ct=img, thumb_ct=thumb)
        _login(client, migrating_user)
        r = client.get(f"/api/v1/migration/voucher-image/{v.id}?size=thumb")
        assert r.status_code == 200
        assert r.data == thumb

    def test_serves_soft_deleted_image(self, db, client, migrating_user):
        ct = b"\x06" * 60
        v = self._seed(db, migrating_user.id, image_ct=ct, deleted=True)
        _login(client, migrating_user)
        r = client.get(f"/api/v1/migration/voucher-image/{v.id}")
        assert r.status_code == 200
        assert r.data == ct

    def test_404_missing(self, db, client, migrating_user):
        _login(client, migrating_user)
        r = client.get("/api/v1/migration/voucher-image/999999")
        assert r.status_code == 404

    def test_404_when_file_absent(self, db, client, migrating_user):
        # image_key はあるがストレージに実体がない → FileNotFoundError → 404
        v = make_voucher(db, migrating_user.id,
                         image_key=f"vouchers/{migrating_user.id}/missing.bin")
        _login(client, migrating_user)
        r = client.get(f"/api/v1/migration/voucher-image/{v.id}")
        assert r.status_code == 404

    def test_idor_other_user_404(self, db, client, migrating_user, second_user):
        v = self._seed(db, second_user.id)
        _login(client, migrating_user)
        r = client.get(f"/api/v1/migration/voucher-image/{v.id}")
        assert r.status_code == 404

    def test_bearer_rejected(self, db, client, migrating_user, api_key_raw):
        v = self._seed(db, migrating_user.id)
        raw_key, _ = api_key_raw
        r = client.get(f"/api/v1/migration/voucher-image/{v.id}",
                       headers=_auth_header(raw_key))
        assert r.status_code == 403

    def test_auditor_rejected(self, db, client, auditor):
        _login(client, auditor)
        r = client.get("/api/v1/migration/voucher-image/1")
        assert r.status_code == 403
