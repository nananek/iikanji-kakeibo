"""証憑画像アクセス権限のセキュリティテスト"""

import json

import pytest

from app.extensions import db
from app.models.ai_draft import AIDraft
from app.models.voucher import Voucher
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.storage import get_storage_backend
from tests.conftest import make_journal, make_voucher


def _setup_voucher_with_file(db_sess, user, accounts):
    """ファイル付き証憑を作成するヘルパー"""
    entry = make_journal(
        db_sess, user.id, "5010", "1010", 500,
        source="ai_receipt",
    )
    image_key = f"vouchers/{user.id}/sec_test.jpg"
    get_storage_backend().put(image_key, b"secure-jpeg", "image/jpeg")
    v = Voucher(
        user_id=user.id,
        journal_entry_id=entry.id,
        image_key=image_key,
    )
    db_sess.session.add(v)
    db_sess.session.commit()
    return v, entry


def _setup_draft_with_file(db_sess, user_id):
    """ファイル付きドラフトを作成するヘルパー"""
    image_key = f"vouchers/{user_id}/draft_test.jpg"
    get_storage_backend().put(image_key, b"draft-jpeg", "image/jpeg")
    draft = AIDraft(
        user_id=user_id,
        image_key=image_key,
        image_mime="image/jpeg",
        status="analyzed",
    )
    db_sess.session.add(draft)
    db_sess.session.commit()
    return draft


class TestVoucherImageUnauthenticated:
    """未ログインでの画像アクセス"""

    def test_voucher_image_unauthenticated(self, client, db, user, accounts):
        """未ログインで証憑画像 → ログインページにリダイレクト"""
        v, _ = _setup_voucher_with_file(db, user, accounts)
        resp = client.get(f"/ai-journal/voucher/{v.id}/image")
        assert resp.status_code in (302, 401)

    def test_draft_image_unauthenticated(self, client, db, user):
        """未ログインでドラフト画像 → ログインページにリダイレクト"""
        draft = _setup_draft_with_file(db, user.id)
        resp = client.get(f"/ai-journal/drafts/{draft.id}/image")
        assert resp.status_code in (302, 401)

    def test_api_voucher_image_no_auth(self, client, db, user, accounts):
        """API: Authorization ヘッダなし → 401"""
        v, _ = _setup_voucher_with_file(db, user, accounts)
        resp = client.get(f"/api/v1/vouchers/{v.id}/image")
        assert resp.status_code == 401


class TestVoucherImageIDOR:
    """他ユーザーの証憑・ドラフト画像への IDOR"""

    def test_voucher_image_other_user(self, app, db, user, accounts, second_user):
        """他ユーザーの証憑画像 → 403"""
        v, _ = _setup_voucher_with_file(db, user, accounts)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(second_user.id)
        resp = client.get(f"/ai-journal/voucher/{v.id}/image")
        assert resp.status_code == 403

    def test_draft_image_other_user(self, app, db, user, second_user):
        """他ユーザーのドラフト画像 → 403"""
        draft = _setup_draft_with_file(db, user.id)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(second_user.id)
        resp = client.get(f"/ai-journal/drafts/{draft.id}/image")
        assert resp.status_code == 403

    def test_api_voucher_image_other_user(self, client, db, user, accounts,
                                          second_user, second_user_accounts):
        """API: 他ユーザーの証憑画像 → 404（区別しない）"""
        from app.models.api_key import APIKey
        v, _ = _setup_voucher_with_file(db, user, accounts)

        # second_user の API キーを作成
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=second_user.id,
            name="other-key",
            key_hash=key_hash,
            key_prefix=key_prefix,
            scopes="journals:read",
            is_active=True,
        )
        db.session.add(key)
        db.session.commit()

        resp = client.get(
            f"/api/v1/vouchers/{v.id}/image",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 404

    def test_api_voucher_verify_other_user(self, client, db, user, accounts,
                                            second_user, second_user_accounts):
        """API: 他ユーザーの証憑検証 → 404"""
        from app.models.api_key import APIKey
        v, _ = _setup_voucher_with_file(db, user, accounts)

        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=second_user.id,
            name="other-key",
            key_hash=key_hash,
            key_prefix=key_prefix,
            scopes="journals:read",
            is_active=True,
        )
        db.session.add(key)
        db.session.commit()

        resp = client.get(
            f"/api/v1/vouchers/{v.id}/verify",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 404


class TestOrphanVoucherImage:
    """孤立証憑（仕訳削除後）のアクセス"""

    def test_orphan_voucher_own_user(self, db, logged_in_client, user, accounts):
        """自分の孤立証憑画像にはアクセスできる"""
        v, entry = _setup_voucher_with_file(db, user, accounts)
        voucher_id = v.id

        # 仕訳を削除 → 証憑は SET NULL で残る
        JournalEntryLine.query.filter_by(journal_entry_id=entry.id).delete()
        db.session.delete(entry)
        db.session.commit()

        orphan = db.session.get(Voucher, voucher_id)
        assert orphan is not None
        assert orphan.journal_entry_id is None

        resp = logged_in_client.get(f"/ai-journal/voucher/{voucher_id}/image")
        assert resp.status_code == 200

    def test_orphan_voucher_other_user(self, app, db, user, accounts, second_user):
        """他ユーザーの孤立証憑画像にはアクセスできない"""
        v, entry = _setup_voucher_with_file(db, user, accounts)
        voucher_id = v.id

        JournalEntryLine.query.filter_by(journal_entry_id=entry.id).delete()
        db.session.delete(entry)
        db.session.commit()

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(second_user.id)
        resp = client.get(f"/ai-journal/voucher/{voucher_id}/image")
        assert resp.status_code == 403


class TestVoucherImageNotFound:
    """存在しないIDへのアクセス"""

    def test_voucher_not_found(self, db, logged_in_client):
        resp = logged_in_client.get("/ai-journal/voucher/99999/image")
        assert resp.status_code == 404

    def test_draft_not_found(self, db, logged_in_client):
        resp = logged_in_client.get("/ai-journal/drafts/99999/image")
        assert resp.status_code == 404

    def test_api_voucher_not_found(self, client, db, user, auth_header):
        resp = client.get("/api/v1/vouchers/99999/image", headers=auth_header)
        assert resp.status_code == 404
