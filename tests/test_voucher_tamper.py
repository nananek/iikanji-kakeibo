"""証憑改ざん防止（Phase 3: 電帳法）テスト"""

import json
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
            action="orphaned",
        )
        db.session.add(log)
        db.session.commit()

        assert log.id is not None
        assert log.action == "orphaned"
        assert log.voucher_id == v.id

    def test_log_with_detail(self, db, user):
        v = make_voucher(db, user.id)
        detail = json.dumps({"journal_entry_id": 42})
        log = VoucherAuditLog(
            voucher_id=v.id,
            user_id=user.id,
            action="orphaned",
            detail=detail,
        )
        db.session.add(log)
        db.session.commit()

        assert json.loads(log.detail)["journal_entry_id"] == 42


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
        detail = json.loads(logs[0].detail)
        assert detail["journal_entry_id"] == entry.id

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


class TestAuditLogAPI:
    """操作ログ API のテスト"""

    def test_api_logs(self, client, db, user, auth_header):
        v = make_voucher(db, user.id)
        db.session.add(VoucherAuditLog(
            voucher_id=v.id, user_id=user.id, action="deleted",
        ))
        db.session.add(VoucherAuditLog(
            voucher_id=v.id, user_id=user.id, action="orphaned",
            detail=json.dumps({"journal_entry_id": 1}),
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
