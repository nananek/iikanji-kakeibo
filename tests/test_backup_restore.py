"""v5 BU-4b: 全置換 restore のテスト。"""

import base64
import json
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.api_key import APIKey
from app.models.balance_cache import BalanceCacheBlob
from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.medical import MedicalExpense
from app.models.user import User
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog


def _auth_header(raw_key):
    return {"Authorization": f"Bearer {raw_key}"}


@pytest.fixture
def reset_limiter(app):
    try:
        from app.extensions import limiter
        limiter.reset()
    except Exception:
        pass
    yield


# --- 認証 / 認可 ---


class TestBackupRestoreAuth:
    def test_unauthenticated_rejected(self, client, reset_limiter):
        resp = client.post("/api/v1/backup/restore", json={})
        assert resp.status_code == 401

    def test_scope_required(self, client, db, user, reset_limiter):
        """backup:restore を持たない API キーでは 403。"""
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="no-restore-scope",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:read", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.post(
            "/api/v1/backup/restore",
            headers=_auth_header(raw_key),
            json={"version": "1.0", "user_id": user.id, "data": {}},
        )
        assert resp.status_code == 403

    def test_auditor_blocked(
        self, app, client, db, auditor, reset_limiter,
    ):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.post(
            "/api/v1/backup/restore",
            json={"version": "1.0", "user_id": auditor.id, "data": {}},
        )
        assert resp.status_code == 403


# --- バリデーション ---


class TestBackupRestoreValidation:
    def test_non_dict_payload(
        self, client, db, user, auth_header, reset_limiter,
    ):
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header,
            data="not json",
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_version_mismatch(
        self, client, db, user, auth_header, reset_limiter, backup_skeleton,
    ):
        backup_skeleton["version"] = "0.9"
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400
        assert "version" in resp.get_json().get("error", "")

    def test_user_id_mismatch_blocks_idor(
        self, client, db, user, auth_header, reset_limiter, backup_skeleton,
    ):
        """backup.user_id が自分でなければ拒否 (IDOR 防御)。"""
        backup_skeleton["user_id"] = user.id + 99999
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400

    def test_missing_data_key(
        self, client, db, user, auth_header, reset_limiter,
    ):
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header,
            json={"version": "1.0", "user_id": user.id},
        )
        assert resp.status_code == 400

    def test_fk_violation_rejected_and_rolled_back(
        self, client, db, user, accounts, auth_header, reset_limiter,
        backup_skeleton,
    ):
        """lines[].journal_entry_id が backup の entries に存在しない場合 400 +
        既存データが何も変わらないこと。"""
        # 事前データ
        from tests.conftest import make_journal
        make_journal(
            db, user.id, "5010", "1010", 1000,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        before_entries = JournalEntry.query.filter_by(user_id=user.id).count()
        before_lines = JournalEntryLine.query.filter_by(
            account_user_id=user.id,
        ).count()
        assert before_entries == 1 and before_lines == 2

        backup_skeleton["data"]["accounts"] = [
            {"code": "1010", "name": "現金", "account_type_id": 1},
        ]
        backup_skeleton["data"]["journal_entry_lines"] = [
            {
                "id": 1, "journal_entry_id": 999,  # 不在
                "account_code": "1010",
                "debit_amount": 100, "credit_amount": 0,
            },
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400
        # rollback されて既存データが残る
        assert JournalEntry.query.filter_by(user_id=user.id).count() == before_entries
        assert JournalEntryLine.query.filter_by(
            account_user_id=user.id,
        ).count() == before_lines

    def test_invalid_account_code_rejected(
        self, client, db, user, auth_header, reset_limiter, backup_skeleton,
    ):
        backup_skeleton["data"]["accounts"] = [
            {"code": "1010", "name": "現金", "account_type_id": 1},
        ]
        backup_skeleton["data"]["journal_entries"] = [
            {"id": 1, "date": "2026-01-01", "entry_number": 1, "description": "x"},
        ]
        backup_skeleton["data"]["journal_entry_lines"] = [
            {
                "id": 1, "journal_entry_id": 1,
                "account_code": "9999",  # accounts に含まれない
                "debit_amount": 100, "credit_amount": 0,
            },
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400


# --- ハッピーパス ---


class TestBackupRestoreHappyPath:
    def _make_minimal_backup(self, user, accounts):
        """1 仕訳 + 2 lines + 1 fiscal_close を含む最小 backup。"""
        return {
            "version": "1.0",
            "exported_at": "2026-02-15T00:00:00+00:00",
            "user_id": user.id,
            "data": {
                "accounts": [
                    {
                        "code": "1010",
                        "name": "現金", "account_type_id": accounts["1010"].account_type_id,
                        "is_system": False, "is_active": True, "display_order": 0,
                    },
                    {
                        "code": "5010",
                        "name": "食費", "account_type_id": accounts["5010"].account_type_id,
                        "is_system": False, "is_active": True, "display_order": 0,
                    },
                ],
                "fiscal_closes": [
                    {"year": 2026, "closed_period": 5},
                ],
                "journal_entries": [
                    {
                        "id": 100, "date": "2026-02-15",
                        "entry_number": 1, "description": "テスト仕訳",
                        "source": "journal", "fiscal_year": 2026,
                    },
                ],
                "journal_entry_lines": [
                    {
                        "id": 200, "journal_entry_id": 100,
                        "account_code": "5010",
                        "debit_amount": 1000, "credit_amount": 0,
                    },
                    {
                        "id": 201, "journal_entry_id": 100,
                        "account_code": "1010",
                        "debit_amount": 0, "credit_amount": 1000,
                    },
                ],
                "medical_expenses": [],
                "balance_cache_blobs": [],
                "vouchers": [],
                "ai_drafts": [],
                "user_ai_config": None,
                "webhook_configs": [],
                "tax_form_mappings": [],
                "csv_column_profiles": [],
            },
        }

    def test_empty_user_restore_succeeds(
        self, client, db, user, accounts, auth_header, reset_limiter,
    ):
        # 既存 (accounts fixture 由来) を上書きして 2 科目 + 1 仕訳に置換
        backup = self._make_minimal_backup(user, accounts)
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup,
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data["ok"] is True
        assert data["restored"]["tables"]["accounts"] == 2
        assert data["restored"]["tables"]["journal_entries"] == 1
        assert data["restored"]["tables"]["journal_entry_lines"] == 2
        assert data["restored"]["tables"]["fiscal_closes"] == 1
        # DB 確認
        assert Account.query.filter_by(user_id=user.id).count() == 2
        assert JournalEntry.query.filter_by(user_id=user.id).count() == 1
        assert JournalEntryLine.query.filter_by(
            account_user_id=user.id,
        ).count() == 2
        # 新 PK が振られる (元の id=100, 200, 201 は使わない)
        entry = JournalEntry.query.filter_by(user_id=user.id).first()
        assert entry.id != 100
        # FK 整合: line.journal_entry_id == 新 entry.id
        lines = JournalEntryLine.query.filter_by(
            journal_entry_id=entry.id,
        ).all()
        assert len(lines) == 2

    def test_replaces_existing_data(
        self, client, db, user, accounts, auth_header, reset_limiter,
    ):
        """既存データが消えて backup 適用されること。"""
        from tests.conftest import make_journal
        # 事前データ (異なる科目 / 仕訳)
        make_journal(
            db, user.id, "5020", "1010", 9999,
            entry_date=date(2025, 12, 31), source="cashbook",
            description="既存仕訳",
        )
        db.session.add(FiscalClose(user_id=user.id, year=2025, closed_period=10))
        db.session.commit()

        backup = self._make_minimal_backup(user, accounts)
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup,
        )
        assert resp.status_code == 200

        # 「既存仕訳」が消えていること
        existing_q = JournalEntry.query.filter_by(
            user_id=user.id, description="既存仕訳",
        ).count()
        assert existing_q == 0
        # backup の「テスト仕訳」のみ存在
        new_q = JournalEntry.query.filter_by(
            user_id=user.id, description="テスト仕訳",
        ).count()
        assert new_q == 1
        # 既存 fiscal_close (year=2025) が消えて backup の 2026 に置き換わる
        fcs = FiscalClose.query.filter_by(user_id=user.id).all()
        assert len(fcs) == 1
        assert fcs[0].year == 2026 and fcs[0].closed_period == 5


# --- 電帳法保管 ---


class TestBackupRestoreAuditLog:
    def test_preserves_voucher_audit_log(
        self, client, db, user, accounts, auth_header, reset_limiter,
    ):
        """VoucherAuditLog 行は残り、user_id が NULL 化される。"""
        log = VoucherAuditLog(
            voucher_id=None, user_id=user.id,
            action="orphaned", detail="{}",
        )
        db.session.add(log)
        db.session.commit()
        log_id = log.id

        backup = {
            "version": "1.0", "user_id": user.id,
            "data": {
                "accounts": [], "fiscal_closes": [],
                "journal_entries": [], "journal_entry_lines": [],
                "medical_expenses": [], "balance_cache_blobs": [],
                "vouchers": [], "ai_drafts": [],
                "user_ai_config": None, "webhook_configs": [],
                "tax_form_mappings": [], "csv_column_profiles": [],
            },
        }
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup,
        )
        assert resp.status_code == 200
        preserved = db.session.get(VoucherAuditLog, log_id)
        assert preserved is not None
        assert preserved.user_id is None  # 匿名化
        assert preserved.action == "orphaned"


# --- 画像 ---


class TestBackupRestoreImages:
    def test_voucher_image_restored_to_storage(
        self, app, client, db, user, accounts, auth_header,
        reset_limiter, monkeypatch,
    ):
        """voucher.image_data が storage に書き戻され、thumbnail も生成。"""
        from app.services import storage as storage_module
        from app.services import backup_restore as br_module

        stored: dict = {}

        class FakeBackend:
            def put(self, key, payload, mime):
                stored[key] = (payload, mime)

            def delete(self, key):
                stored.pop(key, None)

            def get(self, key):
                v = stored.get(key)
                return v[0] if v else None

        backend = FakeBackend()
        monkeypatch.setattr(
            storage_module, "get_storage_backend", lambda: backend,
        )
        monkeypatch.setattr(
            br_module, "get_storage_backend", lambda: backend,
        )
        # サムネ生成は実装に任せる (PIL が必要なので最小 JPEG を使う)
        from PIL import Image
        import io
        img = Image.new("RGB", (1, 1), color="white")
        bio = io.BytesIO()
        img.save(bio, "JPEG")
        jpeg_bytes = bio.getvalue()
        jpeg_b64 = base64.b64encode(jpeg_bytes).decode()

        backup = {
            "version": "1.0", "user_id": user.id,
            "data": {
                "accounts": [], "fiscal_closes": [],
                "journal_entries": [], "journal_entry_lines": [],
                "medical_expenses": [], "balance_cache_blobs": [],
                "vouchers": [
                    {
                        "id": 7, "journal_entry_id": None,
                        "image_key": "old/key.jpg",  # 新規 PK で書き直される
                        "image_mime": "image/jpeg",
                        "image_data": jpeg_b64,
                        "file_hash": "a" * 64,
                        "file_size": len(jpeg_bytes),
                    },
                ],
                "ai_drafts": [],
                "user_ai_config": None, "webhook_configs": [],
                "tax_form_mappings": [], "csv_column_profiles": [],
            },
        }
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup,
        )
        assert resp.status_code == 200, resp.get_json()
        result = resp.get_json()["restored"]
        assert result["tables"]["vouchers"] == 1
        assert result["storage"]["vouchers"] == 1
        # voucher が DB に存在し、image_key が新規 (= user_id を含む) になっている
        vs = Voucher.active().filter_by(user_id=user.id).all()
        assert len(vs) == 1
        assert vs[0].image_key.startswith(f"vouchers/{user.id}/")
        # storage に画像 + サムネが書かれている
        assert vs[0].image_key in stored
        from app.services.storage import make_thumbnail_key
        assert make_thumbnail_key(vs[0].image_key) in stored


# --- 障害系 ---


class TestBackupRestoreFailure:
    def test_storage_failure_rolls_back_db(
        self, app, client, db, user, accounts, auth_header,
        reset_limiter, monkeypatch,
    ):
        from app.services import backup_restore as br_module

        def boom(*a, **kw):
            raise IOError("storage gone")

        monkeypatch.setattr(br_module, "store_image_with_thumbnail", boom)

        from PIL import Image
        import io
        img = Image.new("RGB", (1, 1))
        bio = io.BytesIO()
        img.save(bio, "JPEG")
        jpeg_b64 = base64.b64encode(bio.getvalue()).decode()

        # 事前データを記録
        before_acc = Account.query.filter_by(user_id=user.id).count()
        # backup には existing と異なる新規 account code を入れて区別
        backup = {
            "version": "1.0", "user_id": user.id,
            "data": {
                "accounts": [
                    {"code": "7777", "name": "新規科目-restore-test",
                     "account_type_id": accounts["1010"].account_type_id},
                ],
                "fiscal_closes": [],
                "journal_entries": [], "journal_entry_lines": [],
                "medical_expenses": [], "balance_cache_blobs": [],
                "vouchers": [
                    {
                        "id": 1, "image_key": "x.jpg",
                        "image_mime": "image/jpeg",
                        "image_data": jpeg_b64,
                    },
                ],
                "ai_drafts": [], "user_ai_config": None,
                "webhook_configs": [], "tax_form_mappings": [],
                "csv_column_profiles": [],
            },
        }
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup,
        )
        assert resp.status_code == 500
        # 新規 INSERT が rollback されている → "7777" は存在しない
        new_account = Account.query.filter_by(
            user_id=user.id, code="7777",
        ).first()
        assert new_account is None
        # 削除した既存 accounts も rollback で復活している
        after_acc = Account.query.filter_by(user_id=user.id).count()
        assert after_acc == before_acc
