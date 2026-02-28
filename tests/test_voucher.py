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
            image_mime="image/jpeg",
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
            image_mime="image/jpeg",
        )
        db.session.add(v)
        db.session.commit()
        assert v.journal_entry_id is None

    def test_voucher_relationship(self, db, user, accounts):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
            source="ai_receipt",
        )
        v = make_voucher(db, user.id, journal_entry_id=entry.id)
        assert len(entry.vouchers) == 1
        assert entry.vouchers[0].id == v.id


class TestSetNullOnJournalDelete:
    """仕訳削除時に証憑が SET NULL で残ること"""

    def test_journal_delete_orphans_voucher(self, db, user, accounts):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
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
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
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
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
            source="ai_receipt",
        )

        voucher = create_voucher_from_draft(draft, entry.id)
        db.session.commit()

        assert voucher.id is not None
        assert voucher.journal_entry_id == entry.id
        assert voucher.image_key == "vouchers/1/42.jpg"
        assert voucher.image_mime == "image/jpeg"
        assert voucher.file_hash == "b" * 64
        assert voucher.uploaded_at is not None

        assert db.session.get(AIDraft, draft_id) is None

    def test_voucher_inherits_draft_created_at(self, db, user, accounts):
        draft = self._make_draft(db, user.id)
        original_time = draft.created_at
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
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
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
            source="ai_receipt",
        )
        image_key = f"vouchers/{user.id}/test.jpg"
        get_storage_backend().put(image_key, b"fake-jpeg-data", "image/jpeg")
        v = Voucher(
            user_id=user.id,
            journal_entry_id=entry.id,
            image_key=image_key,
            image_mime="image/jpeg",
        )
        db.session.add(v)
        db.session.commit()
        return v

    def test_voucher_image_returns_data(self, db, logged_in_client, user, accounts):
        v = self._setup_voucher_with_file(db, user, accounts)
        resp = logged_in_client.get(f"/ai-journal/voucher/{v.id}/image")
        assert resp.status_code == 200
        assert resp.data == b"fake-jpeg-data"
        assert resp.content_type == "image/jpeg"

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
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
            source="ai_receipt",
        )
        image_key = f"vouchers/{user.id}/api_test.jpg"
        get_storage_backend().put(image_key, b"api-jpeg", "image/jpeg")
        v = Voucher(
            user_id=user.id,
            journal_entry_id=entry.id,
            image_key=image_key,
            image_mime="image/jpeg",
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
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
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
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
        )
        resp = client.get(f"/api/v1/journals/{entry.id}", headers=auth_header)
        data = resp.get_json()["journal"]
        assert data["vouchers"] == []


class TestJournalFormVoucherPreview:
    """仕訳編集画面の証憑プレビュー表示テスト"""

    def test_edit_with_voucher_shows_image(self, db, logged_in_client, user, accounts):
        """証憑ありの仕訳編集画面でrenderVoucherPreview呼び出しが含まれる"""
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
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
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
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
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
            source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=entry.id)

        resp = logged_in_client.get(f"/journal/{entry.id}/json")
        data = resp.get_json()
        assert data["has_voucher"] is True

    def test_has_voucher_false(self, db, logged_in_client, user, accounts):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
        )

        resp = logged_in_client.get(f"/journal/{entry.id}/json")
        data = resp.get_json()
        assert data["has_voucher"] is False
