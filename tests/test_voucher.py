"""証憑（Voucher）テスト — 電帳法対応の永続保存"""

import hashlib
from datetime import date, datetime, timezone

import pytest

from app.extensions import db
from app.models.voucher import Voucher
from app.models.ai_draft import AIDraft
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.voucher import create_voucher_from_draft
from tests.conftest import make_journal, make_voucher


class TestVoucherModel:
    """Voucher モデルの基本テスト"""

    def test_voucher_creation(self, db, user):
        v = Voucher(
            user_id=user.id,
            image_key="vouchers/1/99.jpg",
            file_hash="a" * 64,
        )
        db.session.add(v)
        db.session.commit()
        assert v.id is not None
        assert v.uploaded_at is not None
        assert v.created_at is not None

    def test_voucher_nullable_journal_entry_id(self, db, user):
        """journal_entry_id なしで作成可能（孤立証憑）"""
        v = Voucher(
            user_id=user.id,
            journal_entry_id=None,
            image_key="vouchers/1/99.jpg",
        )
        db.session.add(v)
        db.session.commit()
        assert v.journal_entry_id is None

    def test_voucher_relationship(self, db, user, accounts):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            source="ai_receipt",
        )
        v = make_voucher(db, user.id, journal_entry_id=entry.id)
        assert len(entry.vouchers) == 1
        assert entry.vouchers[0].id == v.id


class TestVoucherE4EncryptedColumns:
    """E4 (#111): 証憑 E2EE 化のための追加カラム (056 マイグレーション)。"""

    def test_new_columns_default_none(self, db, user):
        """既存の作り方では E4 カラムは NULL のまま (dual-write 期)。"""
        v = Voucher(
            user_id=user.id,
            image_key="vouchers/1/1.jpg",
        )
        db.session.add(v)
        db.session.commit()
        assert v.encrypted_meta_blob is None
        assert v.meta_iv is None
        assert v.file_hash_plain is None
        assert v.thumbnail_key is None

    def test_encrypted_meta_round_trip(self, db, user):
        """encrypted_meta_blob / meta_iv / file_hash_plain / thumbnail_key を
        保存・再読込できる。"""
        meta_blob = b"\x01\x02\x03ciphertext+tag"
        meta_iv = bytes(range(12))
        v = Voucher(
            user_id=user.id,
            image_key="vouchers/1/1.bin",
            file_hash="c" * 64,
            encrypted_meta_blob=meta_blob,
            meta_iv=meta_iv,
            file_hash_plain="p" * 64,
            thumbnail_key="vouchers/1/1_thumb.bin",
        )
        db.session.add(v)
        db.session.commit()
        db.session.expire_all()

        reloaded = db.session.get(Voucher, v.id)
        assert reloaded.encrypted_meta_blob == meta_blob
        assert reloaded.meta_iv == meta_iv
        assert len(reloaded.meta_iv) == 12
        assert reloaded.file_hash_plain == "p" * 64
        assert reloaded.file_hash == "c" * 64
        assert reloaded.thumbnail_key == "vouchers/1/1_thumb.bin"

    def test_audit_log_encrypted_detail_round_trip(self, db, user):
        """VoucherAuditLog の encrypted_detail_blob / detail_iv を保存できる。"""
        from app.models.voucher_audit_log import VoucherAuditLog

        v = make_voucher(db, user.id)
        detail_blob = b"encrypted-detail-json"
        detail_iv = bytes(range(12))
        log = VoucherAuditLog(
            voucher_id=v.id,
            user_id=user.id,
            action="attached",
            encrypted_detail_blob=detail_blob,
            detail_iv=detail_iv,
        )
        db.session.add(log)
        db.session.commit()
        db.session.expire_all()

        reloaded = db.session.get(VoucherAuditLog, log.id)
        assert reloaded.encrypted_detail_blob == detail_blob
        assert reloaded.detail_iv == detail_iv


class TestSetNullOnJournalDelete:
    """仕訳削除時に証憑が SET NULL で残ること"""

    def test_journal_delete_orphans_voucher(self, db, user, accounts):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            source="ai_receipt",
        )
        v = make_voucher(db, user.id, journal_entry_id=entry.id)
        voucher_id = v.id

        db.session.delete(entry)
        db.session.commit()

        orphan = db.session.get(Voucher, voucher_id)
        assert orphan is not None
        assert orphan.journal_entry_id is None

    def test_bulk_delete_orphans_vouchers(self, db, user, accounts):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            source="ai_receipt",
        )
        v = make_voucher(db, user.id, journal_entry_id=entry.id)
        voucher_id = v.id

        JournalEntryLine.query.filter_by(journal_entry_id=entry.id).delete()
        db.session.delete(entry)
        db.session.commit()

        orphan = db.session.get(Voucher, voucher_id)
        assert orphan is not None
        assert orphan.journal_entry_id is None


class TestCreateVoucherFromDraft:
    """create_voucher_from_draft ヘルパーのテスト"""

    def _make_draft(self, db, user_id):
        draft = AIDraft(
            user_id=user_id,
            image_key="vouchers/1/42.jpg",
            image_mime="image/jpeg",
            file_hash="b" * 64,
            status="analyzed",
        )
        db.session.add(draft)
        db.session.commit()
        return draft

    def test_creates_voucher_and_deletes_draft(self, db, user, accounts):
        draft = self._make_draft(db, user.id)
        draft_id = draft.id
        entry = make_journal(
            db, user.id, "5010", "1010", 500,
            source="ai_receipt",
        )

        voucher = create_voucher_from_draft(draft, entry.id)
        db.session.commit()

        assert voucher.id is not None
        assert voucher.journal_entry_id == entry.id
        assert voucher.image_key == "vouchers/1/42.jpg"
        assert voucher.file_hash == "b" * 64
        assert voucher.uploaded_at is not None

        assert db.session.get(AIDraft, draft_id) is None

    def test_voucher_inherits_draft_created_at(self, db, user, accounts):
        draft = self._make_draft(db, user.id)
        original_time = draft.created_at
        entry = make_journal(
            db, user.id, "5010", "1010", 500,
            source="ai_receipt",
        )

        voucher = create_voucher_from_draft(draft, entry.id)
        db.session.commit()

        assert voucher.uploaded_at == original_time


class TestVoucherImageEndpoint:
    """証憑画像エンドポイントのテスト"""

    def _setup_voucher_with_file(self, db, user, accounts):
        from app.services.storage import get_storage_backend
        entry = make_journal(
            db, user.id, "5010", "1010", 500,
            source="ai_receipt",
        )
        image_key = f"vouchers/{user.id}/test.jpg"
        get_storage_backend().put(image_key, b"fake-jpeg-data", "image/jpeg")
        v = Voucher(
            user_id=user.id,
            journal_entry_id=entry.id,
            image_key=image_key,
        )
        db.session.add(v)
        db.session.commit()
        return v

    def test_voucher_image_returns_data(self, db, logged_in_client, user, accounts):
        v = self._setup_voucher_with_file(db, user, accounts)
        resp = logged_in_client.get(f"/ai-journal/voucher/{v.id}/image")
        assert resp.status_code == 200
        assert resp.data == b"fake-jpeg-data"
        # E5 PR-5 (#111): image_mime 列廃止により平文証憑も octet-stream 配信
        # (ブラウザが content-sniff)。
        assert resp.content_type == "application/octet-stream"

    def test_voucher_image_wrong_user(self, app, db, user, accounts, second_user):
        v = self._setup_voucher_with_file(db, user, accounts)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(second_user.id)
        resp = client.get(f"/ai-journal/voucher/{v.id}/image")
        assert resp.status_code == 403

    def test_voucher_image_not_found(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.get("/ai-journal/voucher/99999/image")
        assert resp.status_code == 404

    def test_voucher_image_has_cache_headers(self, db, logged_in_client, user, accounts):
        v = self._setup_voucher_with_file(db, user, accounts)
        resp = logged_in_client.get(f"/ai-journal/voucher/{v.id}/image")
        assert resp.status_code == 200
        assert "immutable" in resp.headers.get("Cache-Control", "")

    def test_voucher_image_etag(self, db, logged_in_client, user, accounts):
        v = self._setup_voucher_with_file(db, user, accounts)
        v.file_hash = "a" * 64
        db.session.commit()
        resp = logged_in_client.get(f"/ai-journal/voucher/{v.id}/image")
        assert resp.headers.get("ETag") == f'"{"a" * 64}"'

    def test_voucher_image_304_not_modified(self, db, logged_in_client, user, accounts):
        v = self._setup_voucher_with_file(db, user, accounts)
        v.file_hash = "a" * 64
        db.session.commit()
        resp = logged_in_client.get(
            f"/ai-journal/voucher/{v.id}/image",
            headers={"If-None-Match": f'"{"a" * 64}"'},
        )
        assert resp.status_code == 304

    def test_voucher_image_thumb_fallback(self, db, logged_in_client, user, accounts):
        v = self._setup_voucher_with_file(db, user, accounts)
        resp = logged_in_client.get(f"/ai-journal/voucher/{v.id}/image?size=thumb")
        assert resp.status_code == 200


class TestAPIVoucherImage:
    """API 証憑画像エンドポイントのテスト"""

    def test_api_voucher_image(self, client, db, user, accounts, auth_header):
        from app.services.storage import get_storage_backend
        entry = make_journal(
            db, user.id, "5010", "1010", 500,
            source="ai_receipt",
        )
        image_key = f"vouchers/{user.id}/api_test.jpg"
        get_storage_backend().put(image_key, b"api-jpeg", "image/jpeg")
        v = Voucher(
            user_id=user.id,
            journal_entry_id=entry.id,
            image_key=image_key,
        )
        db.session.add(v)
        db.session.commit()

        resp = client.get(f"/api/v1/vouchers/{v.id}/image", headers=auth_header)
        assert resp.status_code == 200
        assert resp.data == b"api-jpeg"

    def test_api_voucher_not_found(self, client, db, user, auth_header):
        resp = client.get("/api/v1/vouchers/99999/image", headers=auth_header)
        assert resp.status_code == 404


class TestEntryToDictVouchers:
    """API レスポンスに vouchers が含まれること"""

    def test_entry_dict_includes_vouchers(self, client, db, user, accounts, auth_header):
        entry = make_journal(
            db, user.id, "5010", "1010", 500,
            source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=entry.id)

        resp = client.get(f"/api/v1/journals/{entry.id}", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()["journal"]
        assert "vouchers" in data
        assert len(data["vouchers"]) == 1
        assert "id" in data["vouchers"][0]
        assert "uploaded_at" in data["vouchers"][0]

    def test_entry_dict_empty_vouchers(self, client, db, user, accounts, auth_header):
        entry = make_journal(
            db, user.id, "5010", "1010", 500,
        )
        resp = client.get(f"/api/v1/journals/{entry.id}", headers=auth_header)
        data = resp.get_json()["journal"]
        assert data["vouchers"] == []


class TestJournalFormVoucherPreview:
    """仕訳編集画面の証憑プレビュー表示テスト"""

    def test_edit_with_voucher_shows_image(self, db, logged_in_client, user, accounts):
        """証憑ありの仕訳編集画面でrenderVoucherPreview呼び出しが含まれる"""
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            source="ai_receipt",
        )
        v = make_voucher(db, user.id, journal_entry_id=entry.id)

        resp = logged_in_client.get(f"/journal/{entry.id}/edit")
        html = resp.data.decode()
        assert "renderVoucherPreview" in html
        assert str(v.id) in html

    def test_edit_without_voucher_no_image(self, db, logged_in_client, user, accounts):
        """証憑なしの仕訳編集画面ではrenderVoucherPreview呼び出しなし"""
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
        )

        resp = logged_in_client.get(f"/journal/{entry.id}/edit")
        html = resp.data.decode()
        assert "renderVoucherPreview" not in html

    def test_new_form_no_voucher_section(self, db, logged_in_client, user, accounts):
        """新規仕訳画面ではrenderVoucherPreview呼び出しなし"""
        resp = logged_in_client.get("/journal/new")
        html = resp.data.decode()
        assert "renderVoucherPreview" not in html


class TestEntryJsonHasVoucher:
    """entry_json の has_voucher フィールドテスト"""

    def test_has_voucher_true(self, db, logged_in_client, user, accounts):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=entry.id)

        resp = logged_in_client.get(f"/journal/{entry.id}/json")
        data = resp.get_json()
        assert data["has_voucher"] is True

    def test_has_voucher_false(self, db, logged_in_client, user, accounts):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
        )

        resp = logged_in_client.get(f"/journal/{entry.id}/json")
        data = resp.get_json()
        assert data["has_voucher"] is False
