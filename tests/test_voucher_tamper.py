"""証憑改ざん防止（Phase 3: 電帳法）テスト"""

import hashlib
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from tests.conftest import make_journal, make_voucher


def _make_voucher_with_hash(db, user_id, journal_entry_id=None,
                            image_key="vouchers/1/1.jpg", file_hash="a" * 64):
    """file_hash 付きの証憑を作成"""
    v = Voucher(
        user_id=user_id,
        journal_entry_id=journal_entry_id,
        image_key=image_key,
        image_mime="image/jpeg",
        file_hash=file_hash,
    )
    db.session.add(v)
    db.session.commit()
    return v


class TestVoucherAuditLogModel:
    """VoucherAuditLog モデルのテスト"""

    def test_create_log(self, db, user):
        v = make_voucher(db, user.id)
        log = VoucherAuditLog(
            voucher_id=v.id,
            user_id=user.id,
            action="hash_verified",
        )
        db.session.add(log)
        db.session.commit()

        assert log.id is not None
        assert log.action == "hash_verified"
        assert log.voucher_id == v.id

    def test_log_with_encrypted_detail(self, db, user):
        # E4 PR-F: 平文 detail 列は DROP 済。クライアント供給の暗号化ノートは
        # encrypted_detail_blob / detail_iv (AAD="valog") に保存する。
        v = make_voucher(db, user.id)
        blob = b"encrypted-note"
        iv = bytes(range(12))
        log = VoucherAuditLog(
            voucher_id=v.id,
            user_id=user.id,
            action="orphaned",
            encrypted_detail_blob=blob,
            detail_iv=iv,
        )
        db.session.add(log)
        db.session.commit()

        assert log.encrypted_detail_blob == blob
        assert log.detail_iv == iv


class TestOrphanLogging:
    """仕訳削除→証憑孤立化ログのテスト"""

    def test_single_delete_logs_orphan(self, db, logged_in_client, user, accounts):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            source="ai_receipt",
        )
        v = _make_voucher_with_hash(db, user.id, journal_entry_id=entry.id)

        resp = logged_in_client.post(f"/journal/{entry.id}/delete")
        assert resp.status_code in (302, 200)

        logs = VoucherAuditLog.query.filter_by(voucher_id=v.id).all()
        assert len(logs) == 1
        assert logs[0].action == "orphaned"
        # E4 PR-D: 平文 detail は書かない。"orphaned" action 自体が「紐付け
        # 仕訳が削除された事実」(電帳法 訂正削除の事実) を担保する。
        assert logs[0].encrypted_detail_blob is None

    def test_bulk_delete_logs_orphan(self, db, logged_in_client, user, accounts):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            source="ai_receipt",
        )
        v = _make_voucher_with_hash(db, user.id, journal_entry_id=entry.id)

        resp = logged_in_client.post("/journal/bulk-delete", data={
            "entry_ids": [entry.id],
        })
        assert resp.status_code in (302, 200)

        logs = VoucherAuditLog.query.filter_by(voucher_id=v.id).all()
        assert len(logs) == 1
        assert logs[0].action == "orphaned"

    def test_api_delete_logs_orphan(self, client, db, user, accounts, auth_header):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            source="ai_receipt",
        )
        v = _make_voucher_with_hash(db, user.id, journal_entry_id=entry.id)

        resp = client.delete(f"/api/v1/journals/{entry.id}", headers=auth_header)
        assert resp.status_code == 200

        logs = VoucherAuditLog.query.filter_by(voucher_id=v.id).all()
        assert len(logs) == 1
        assert logs[0].action == "orphaned"

    def test_no_log_when_no_voucher(self, db, logged_in_client, user, accounts):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
        )
        entry_id = entry.id

        resp = logged_in_client.post(f"/journal/{entry_id}/delete")
        assert resp.status_code in (302, 200)

        logs = VoucherAuditLog.query.all()
        assert len(logs) == 0


class TestHashVerifyWeb:
    """Web UI ハッシュ検証のテスト"""

    def test_verify_success(self, db, logged_in_client, user):
        image_data = b"test image content"
        file_hash = hashlib.sha256(image_data).hexdigest()
        v = _make_voucher_with_hash(db, user.id, file_hash=file_hash)

        with patch("app.views.vouchers.get_storage_backend") as mock_sb:
            mock_sb.return_value.get.return_value = image_data
            resp = logged_in_client.post(f"/vouchers/{v.id}/verify")

        assert resp.status_code == 302
        log = VoucherAuditLog.query.filter_by(voucher_id=v.id).first()
        assert log.action == "hash_verified"

    def test_verify_mismatch(self, db, logged_in_client, user):
        v = _make_voucher_with_hash(db, user.id, file_hash="a" * 64)

        with patch("app.views.vouchers.get_storage_backend") as mock_sb:
            mock_sb.return_value.get.return_value = b"tampered content"
            resp = logged_in_client.post(f"/vouchers/{v.id}/verify")

        assert resp.status_code == 302
        log = VoucherAuditLog.query.filter_by(voucher_id=v.id).first()
        assert log.action == "hash_mismatch"

    def test_verify_no_hash(self, db, logged_in_client, user):
        v = make_voucher(db, user.id)  # file_hash=None

        resp = logged_in_client.post(f"/vouchers/{v.id}/verify")
        assert resp.status_code == 302

        logs = VoucherAuditLog.query.filter_by(voucher_id=v.id).all()
        assert len(logs) == 0


class TestHashVerifyAPI:
    """API ハッシュ検証のテスト"""

    def test_api_verify_success(self, client, db, user, auth_header):
        image_data = b"api test image"
        file_hash = hashlib.sha256(image_data).hexdigest()
        v = _make_voucher_with_hash(db, user.id, file_hash=file_hash)

        with patch("app.views.api.get_storage_backend") as mock_sb:
            mock_sb.return_value.get.return_value = image_data
            resp = client.get(
                f"/api/v1/vouchers/{v.id}/verify",
                headers=auth_header,
            )

        data = resp.get_json()
        assert data["ok"] is True
        assert data["verified"] is True
        assert data["stored_hash"] == file_hash

    def test_api_verify_mismatch(self, client, db, user, auth_header):
        v = _make_voucher_with_hash(db, user.id, file_hash="b" * 64)

        with patch("app.views.api.get_storage_backend") as mock_sb:
            mock_sb.return_value.get.return_value = b"different content"
            resp = client.get(
                f"/api/v1/vouchers/{v.id}/verify",
                headers=auth_header,
            )

        data = resp.get_json()
        assert data["ok"] is True
        assert data["verified"] is False

    def test_api_verify_no_hash(self, client, db, user, auth_header):
        v = make_voucher(db, user.id)

        resp = client.get(
            f"/api/v1/vouchers/{v.id}/verify",
            headers=auth_header,
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert data["verified"] is None

    def test_api_verify_other_user(self, client, db, user, second_user, auth_header):
        v = _make_voucher_with_hash(db, second_user.id, file_hash="c" * 64)

        resp = client.get(
            f"/api/v1/vouchers/{v.id}/verify",
            headers=auth_header,
        )
        assert resp.status_code == 404


class TestAuditLogAPI:
    """操作ログ API のテスト"""

    def test_api_logs(self, client, db, user, auth_header):
        v = make_voucher(db, user.id)
        db.session.add(VoucherAuditLog(
            voucher_id=v.id, user_id=user.id, action="hash_verified",
        ))
        db.session.add(VoucherAuditLog(
            voucher_id=v.id, user_id=user.id, action="orphaned",
        ))
        db.session.commit()

        resp = client.get(
            f"/api/v1/vouchers/{v.id}/logs",
            headers=auth_header,
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["logs"]) == 2

    def test_api_logs_other_user(self, client, db, user, second_user, auth_header):
        v = make_voucher(db, second_user.id)

        resp = client.get(
            f"/api/v1/vouchers/{v.id}/logs",
            headers=auth_header,
        )
        assert resp.status_code == 404
