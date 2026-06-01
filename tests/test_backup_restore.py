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

    def test_fiscal_period_16_rejected(
        self, client, db, user, accounts, auth_header, reset_limiter,
        backup_skeleton,
    ):
        """fiscal_period=16 (損益振替) は restore 経由でも禁止。"""
        backup_skeleton["data"]["accounts"] = [
            {"code": "1010", "name": "現金",
             "account_type_id": accounts["1010"].account_type_id},
        ]
        backup_skeleton["data"]["journal_entries"] = [
            {"id": 1, "date": "2026-12-31", "entry_number": 1,
             "description": "悪意の損益振替", "fiscal_period": 16},
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400
        # E3-F: closing 判定メッセージは is_closing ベースに変更。
        assert "損益振替" in resp.get_json().get("error", "")

    def test_unbalanced_entry_rejected(
        self, client, db, user, accounts, auth_header, reset_limiter,
        backup_skeleton,
    ):
        """貸借不一致の仕訳は復元拒否 (改ざん検知)。"""
        eb = base64.b64encode(b"\x42" * 48).decode("ascii")
        iv = base64.b64encode(b"\x42" * 12).decode("ascii")
        backup_skeleton["data"]["accounts"] = [
            {"code": "1010", "name": "現金",
             "account_type_id": accounts["1010"].account_type_id},
            {"code": "5010", "name": "食費",
             "account_type_id": accounts["5010"].account_type_id},
        ]
        backup_skeleton["data"]["journal_entries"] = [
            {"id": 1, "date": "2026-01-01", "entry_number": 1, "description": "x",
             "encrypted_blob": eb, "blob_iv": iv},
        ]
        backup_skeleton["data"]["journal_entry_lines"] = [
            {"id": 1, "journal_entry_id": 1, "account_code": "5010",
             "debit_amount": 1000, "credit_amount": 0,
             "encrypted_blob": eb, "blob_iv": iv},
            # 借方 1000 vs 貸方 999 → 不一致
            {"id": 2, "journal_entry_id": 1, "account_code": "1010",
             "debit_amount": 0, "credit_amount": 999,
             "encrypted_blob": eb, "blob_iv": iv},
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400
        assert "貸借" in resp.get_json().get("error", "")

    def test_restore_rejects_journal_entry_without_encrypted_blob(
        self, client, db, user, accounts, auth_header, reset_limiter,
        backup_skeleton,
    ):
        """PR-C: restore で journal_entries に encrypted_blob 欠落 → 400 拒否。"""
        backup_skeleton["data"]["accounts"] = [
            {"code": "1010", "name": "現金",
             "account_type_id": accounts["1010"].account_type_id},
        ]
        backup_skeleton["data"]["journal_entries"] = [
            # encrypted_blob/blob_iv 欠落
            {"id": 1, "date": "2026-02-15", "entry_number": 1,
             "description": "test"},
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400
        assert "encrypted_blob" in resp.get_json().get("error", "")

    def test_restore_rejects_line_without_encrypted_blob(
        self, client, db, user, accounts, auth_header, reset_limiter,
        backup_skeleton,
    ):
        """PR-C: restore で journal_entry_lines に encrypted_blob 欠落 → 400 拒否。"""
        eb = base64.b64encode(b"\x42" * 48).decode("ascii")
        iv = base64.b64encode(b"\x42" * 12).decode("ascii")
        backup_skeleton["data"]["accounts"] = [
            {"code": "1010", "name": "現金",
             "account_type_id": accounts["1010"].account_type_id},
        ]
        backup_skeleton["data"]["journal_entries"] = [
            {"id": 1, "date": "2026-02-15", "entry_number": 1,
             "description": "test",
             "encrypted_blob": eb, "blob_iv": iv},
        ]
        backup_skeleton["data"]["journal_entry_lines"] = [
            # 暗号化欠落の行
            {"id": 1, "journal_entry_id": 1, "account_code": "1010",
             "debit_amount": 100, "credit_amount": 0},
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400
        assert "encrypted_blob" in resp.get_json().get("error", "")


# --- ハッピーパス ---


class TestBackupRestoreHappyPath:
    def _make_minimal_backup(self, user, accounts):
        """1 仕訳 + 2 lines + 1 fiscal_close を含む最小 backup。

        PR-C 以降 journal_entries / journal_entry_lines / medical_expenses
        には encrypted_blob/blob_iv が必須化されたため、ダミー暗号文を付与する。
        """
        eb = base64.b64encode(b"\x42" * 48).decode("ascii")
        iv = base64.b64encode(b"\x42" * 12).decode("ascii")
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
                        "encrypted_blob": eb, "blob_iv": iv,
                    },
                ],
                "journal_entry_lines": [
                    {
                        "id": 200, "journal_entry_id": 100,
                        "account_code": "5010",
                        "debit_amount": 1000, "credit_amount": 0,
                        "encrypted_blob": eb, "blob_iv": iv,
                    },
                    {
                        "id": 201, "journal_entry_id": 100,
                        "account_code": "1010",
                        "debit_amount": 0, "credit_amount": 1000,
                        "encrypted_blob": eb, "blob_iv": iv,
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

        # 既存データは全削除され backup の 1 件のみ残る。E3-F PR-D-6-4 で平文
        # description は復元しないため、件数 + backup 由来の entry_number /
        # fiscal_year / encrypted_blob で復元仕訳を識別する。
        entries = JournalEntry.query.filter_by(user_id=user.id).all()
        assert len(entries) == 1
        assert entries[0].entry_number == 1
        assert entries[0].fiscal_year == 2026
        assert entries[0].encrypted_blob
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
            action="orphaned",
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

    def test_e2ee_voucher_restored_verbatim(
        self, app, client, db, user, accounts, auth_header,
        reset_limiter, monkeypatch,
    ):
        """E4 (#111) PR-H: E2EE 証憑は暗号文を無加工で保存し、暗号メタ列と
        aad_id を復元する (Pillow を回さない)。サムネは _thumb.bin に暗号文で書く。
        """
        import hashlib
        from app.services import storage as storage_module
        from app.services import backup_restore as br_module
        from app.services.storage import (
            make_encrypted_thumbnail_key, make_thumbnail_key,
        )

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

        # 暗号文 (iv||ct||tag) はサーバから見て opaque なバイト列
        image_ct = b"\x11" * 12 + b"ENCRYPTED-IMAGE-CT" + b"\x22" * 16
        thumb_ct = b"\x33" * 12 + b"ENCRYPTED-THUMB-CT" + b"\x44" * 16
        meta_blob = b"\xaa" * 40
        meta_iv = b"\xbb" * 12
        aad_id = 7_233_445_566_778_899_001  # 63bit 域の安定識別子
        cipher_hash = hashlib.sha256(image_ct).hexdigest()

        backup = {
            "version": "1.0", "user_id": user.id,
            "data": {
                "accounts": [], "fiscal_closes": [],
                "journal_entries": [], "journal_entry_lines": [],
                "medical_expenses": [], "balance_cache_blobs": [],
                "vouchers": [
                    {
                        "id": 9, "journal_entry_id": None,
                        "image_key": "old/9.bin",
                        "image_mime": "application/octet-stream",
                        "image_data": base64.b64encode(image_ct).decode(),
                        "file_hash": cipher_hash,
                        "file_hash_plain": "b" * 64,
                        "file_size": len(image_ct) + len(thumb_ct),
                        "encrypted_meta_blob": base64.b64encode(meta_blob).decode(),
                        "meta_iv": base64.b64encode(meta_iv).decode(),
                        "aad_id": str(aad_id),
                        "thumbnail_data": base64.b64encode(thumb_ct).decode(),
                    },
                ],
                "ai_drafts": [],
                "user_ai_config": None, "webhook_configs": [],
                "tax_form_mappings": [], "csv_column_profiles": [],
            },
        }
        resp = client.post(
            "/api/v1/backup/restore", headers=auth_header, json=backup,
        )
        assert resp.status_code == 200, resp.get_json()

        v = Voucher.active().filter_by(user_id=user.id).one()
        # 暗号メタ列が往復復元されている
        assert v.encrypted_meta_blob == meta_blob
        assert v.meta_iv == meta_iv
        assert v.file_hash_plain == "b" * 64
        assert v.aad_id == aad_id  # AAD 束縛の安定識別子を保持
        assert v.file_size == len(image_ct) + len(thumb_ct)
        # 暗号文は無加工で保存 (Pillow を通っていない)
        assert v.image_key.startswith(f"vouchers/{user.id}/")
        assert v.image_key.endswith(".bin")
        assert stored[v.image_key][0] == image_ct
        # file_hash は cipher hash
        assert v.file_hash == cipher_hash
        # サムネは _thumb.bin に暗号文で保存される
        assert v.thumbnail_key == make_encrypted_thumbnail_key(v.image_key)
        assert stored[v.thumbnail_key][0] == thumb_ct
        # サーバ生成 JPEG サムネ (_thumb.jpg) は作られない
        assert make_thumbnail_key(v.image_key) not in stored

    def test_e2ee_voucher_export_restore_roundtrip(
        self, app, client, db, user, accounts, auth_header,
        reset_limiter, monkeypatch,
    ):
        """E4 (#111) PR-H: export → restore で E2EE 証憑が忠実に往復し、PK が
        再採番されても aad_id と暗号文が保持される。"""
        from app.services import storage as storage_module
        from app.services import backup_restore as br_module
        from app.services.storage import (
            make_storage_key, make_encrypted_thumbnail_key,
            ENCRYPTED_CONTENT_TYPE,
        )

        stored: dict = {}

        class FakeBackend:
            def put(self, key, payload, mime):
                stored[key] = (payload, mime)

            def delete(self, key):
                stored.pop(key, None)

            def get(self, key):
                v = stored.get(key)
                return v[0] if v else None

        from app.views import api as api_module

        backend = FakeBackend()
        monkeypatch.setattr(
            storage_module, "get_storage_backend", lambda: backend,
        )
        monkeypatch.setattr(
            br_module, "get_storage_backend", lambda: backend,
        )
        # export エンドポイント (api.py) は自モジュールに束縛した参照を使う
        monkeypatch.setattr(
            api_module, "get_storage_backend", lambda: backend,
        )

        image_ct = b"\x55" * 12 + b"ROUNDTRIP-IMAGE" + b"\x66" * 16
        thumb_ct = b"\x77" * 12 + b"ROUNDTRIP-THUMB" + b"\x88" * 16
        meta_blob = b"\xcc" * 32
        meta_iv = b"\xdd" * 12
        aad_id = 4_611_686_018_427_387_903  # 2^62-1 近傍

        # DB + storage に E2EE 証憑を仕込む
        img_key = make_storage_key(user.id, 1, ENCRYPTED_CONTENT_TYPE)
        thumb_key = make_encrypted_thumbnail_key(img_key)
        stored[img_key] = (image_ct, ENCRYPTED_CONTENT_TYPE)
        stored[thumb_key] = (thumb_ct, ENCRYPTED_CONTENT_TYPE)
        import hashlib
        v0 = Voucher(
            user_id=user.id, journal_entry_id=None,
            image_key=img_key,
            file_hash=hashlib.sha256(image_ct).hexdigest(),
            file_hash_plain="c" * 64,
            encrypted_meta_blob=meta_blob, meta_iv=meta_iv,
            aad_id=aad_id, thumbnail_key=thumb_key,
            file_size=len(image_ct) + len(thumb_ct),
        )
        db.session.add(v0)
        db.session.commit()

        # export
        exp = client.get("/api/v1/backup/export", headers=auth_header)
        assert exp.status_code == 200
        data = exp.get_json()["data"]
        assert len(data["vouchers"]) == 1
        vd = data["vouchers"][0]
        assert vd["aad_id"] == str(aad_id)
        assert vd["encrypted_meta_blob"] == base64.b64encode(meta_blob).decode()
        assert vd["meta_iv"] == base64.b64encode(meta_iv).decode()
        assert vd["file_hash_plain"] == "c" * 64
        assert vd["thumbnail_data"] == base64.b64encode(thumb_ct).decode()
        assert vd["image_data"] == base64.b64encode(image_ct).decode()

        # restore (export 結果をそのまま投入)
        backup = {"version": "1.0", "user_id": user.id, "data": data}
        resp = client.post(
            "/api/v1/backup/restore", headers=auth_header, json=backup,
        )
        assert resp.status_code == 200, resp.get_json()

        v1 = Voucher.active().filter_by(user_id=user.id).one()
        # PK は再採番され得るが aad_id と暗号文は不変 (= 復号可能性が保たれる)
        assert v1.aad_id == aad_id
        assert v1.encrypted_meta_blob == meta_blob
        assert v1.meta_iv == meta_iv
        assert v1.file_hash_plain == "c" * 64
        assert stored[v1.image_key][0] == image_ct
        assert stored[v1.thumbnail_key][0] == thumb_ct


# --- 障害系 ---


class TestBackupRestoreCoverage:
    """カバレッジ補強用テスト (各 _restore_<table> のハッピーパス + 例外分岐)。"""

    def test_restore_all_optional_tables(
        self, client, db, user, accounts, auth_header, reset_limiter,
        backup_skeleton,
    ):
        """balance_cache_blob / user_ai_config / webhook / tax_mapping /
        csv_profile / medical_expense のハッピーパス restore (カバレッジ補強)。"""
        from app.models.tax_form import TaxFormField
        # tax_form_mappings は TaxFormField の seed が必要
        f = TaxFormField(
            form_type="general", page=1, section="revenue",
            row_code="X", name="売上(restore-test)",
            account_type_code="revenue", display_order=1,
        )
        db.session.add(f)
        db.session.commit()

        eb = base64.b64encode(b"\x42" * 48).decode("ascii")
        iv = base64.b64encode(b"\x42" * 12).decode("ascii")
        backup_skeleton["data"]["accounts"] = [
            {"code": "1010", "name": "現金",
             "account_type_id": accounts["1010"].account_type_id},
        ]
        backup_skeleton["data"]["journal_entries"] = [
            {"id": 1, "date": "2026-02-15", "entry_number": 1,
             "description": "test",
             "encrypted_blob": eb, "blob_iv": iv},
        ]
        backup_skeleton["data"]["medical_expenses"] = [
            {"id": 1, "journal_entry_id": 1, "date": "2026-02-15",
             "patient_name": "本人", "hospital_name": "病院",
             "amount_paid": 5000, "insurance_reimbursement": 1000,
             "encrypted_blob": eb, "blob_iv": iv},
        ]
        backup_skeleton["data"]["balance_cache_blobs"] = [
            {"year": 2026, "period": 12,
             "encrypted_blob": base64.b64encode(b"\x01\x02").decode(),
             "blob_iv": base64.b64encode(b"\x00" * 12).decode()},
        ]
        backup_skeleton["data"]["user_ai_config"] = {
            "provider": "openai", "model_name": "gpt-4o-mini",
            "api_key_blob": base64.b64encode(b"\xaa").decode(),
            "api_key_iv": base64.b64encode(b"\x00" * 12).decode(),
            "custom_prompt": "", "compliance_check": True,
        }
        backup_skeleton["data"]["webhook_configs"] = [
            {"name": "Discord", "provider": "discord",
             "webhook_url": "https://x.example/hook",
             "events_json": '["import_success"]'},
        ]
        backup_skeleton["data"]["tax_form_mappings"] = [
            {"account_code": "1010", "field_id": f.id},
        ]
        backup_skeleton["data"]["csv_column_profiles"] = [
            {"account_code": "1010",
             "date_col": 0, "desc_col": 1,
             "deposit_col": 2, "withdrawal_col": 3,
             "date_format": "%Y-%m-%d", "amount_mode": "separate"},
        ]

        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 200, resp.get_json()
        r = resp.get_json()["restored"]
        assert r["tables"]["medical_expenses"] == 1
        assert r["tables"]["balance_cache_blobs"] == 1
        assert r["tables"]["user_ai_config"] == 1
        assert r["tables"]["webhook_configs"] == 1
        assert r["tables"]["tax_form_mappings"] == 1
        assert r["tables"]["csv_column_profiles"] == 1

    def test_restore_ai_draft_with_image(
        self, app, client, db, user, auth_header, reset_limiter,
        backup_skeleton, monkeypatch,
    ):
        """AIDraft 画像の storage 書き戻し (カバレッジ補強)。"""
        from app.services import storage as storage_module
        from app.services import backup_restore as br_module

        stored: dict = {}

        class FakeBackend:
            def put(self, key, payload, mime):
                stored[key] = payload

            def delete(self, key):
                stored.pop(key, None)

            def get(self, key):
                return stored.get(key)

        backend = FakeBackend()
        monkeypatch.setattr(
            storage_module, "get_storage_backend", lambda: backend,
        )
        monkeypatch.setattr(
            br_module, "get_storage_backend", lambda: backend,
        )
        from PIL import Image
        import io
        img = Image.new("RGB", (1, 1))
        bio = io.BytesIO()
        img.save(bio, "JPEG")
        jpeg_b64 = base64.b64encode(bio.getvalue()).decode()

        backup_skeleton["data"]["ai_drafts"] = [
            {"id": 1, "image_key": "old/key.jpg",
             "image_mime": "image/jpeg", "image_data": jpeg_b64,
             "status": "pending", "comment": "メモ"},
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 200
        assert resp.get_json()["restored"]["tables"]["ai_drafts"] == 1
        from app.models.ai_draft import AIDraft
        ds = AIDraft.query.filter_by(user_id=user.id).all()
        assert len(ds) == 1
        assert ds[0].image_key.startswith(f"vouchers/{user.id}/")

    def test_invalid_base64_rejected(
        self, client, db, user, auth_header, reset_limiter, backup_skeleton,
    ):
        """base64 として decode できない値は 400。"""
        backup_skeleton["data"]["journal_entries"] = [
            {"id": 1, "date": "2026-01-01", "entry_number": 1,
             "description": "x", "encrypted_blob": "!!!not-base64!!!"},
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400

    def test_invalid_year_range_rejected(
        self, client, db, user, auth_header, reset_limiter, backup_skeleton,
    ):
        backup_skeleton["data"]["balance_cache_blobs"] = [
            {"year": 1800, "period": 5,
             "encrypted_blob": base64.b64encode(b"x").decode(),
             "blob_iv": base64.b64encode(b"\x00" * 12).decode()},
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400
        assert "year" in resp.get_json().get("error", "")

    def test_invalid_period_rejected(
        self, client, db, user, auth_header, reset_limiter, backup_skeleton,
    ):
        backup_skeleton["data"]["balance_cache_blobs"] = [
            {"year": 2026, "period": 99,
             "encrypted_blob": base64.b64encode(b"x").decode(),
             "blob_iv": base64.b64encode(b"\x00" * 12).decode()},
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400
        assert "period" in resp.get_json().get("error", "")

    def test_list_table_must_be_list(
        self, client, db, user, auth_header, reset_limiter, backup_skeleton,
    ):
        backup_skeleton["data"]["accounts"] = "not a list"
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400

    def test_user_ai_config_must_be_dict_or_null(
        self, client, db, user, auth_header, reset_limiter, backup_skeleton,
    ):
        backup_skeleton["data"]["user_ai_config"] = "wrong type"
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 400

    def test_voucher_without_image_data_skipped(
        self, app, client, db, user, accounts, auth_header,
        reset_limiter, monkeypatch, backup_skeleton,
    ):
        """export 時に画像取得失敗で image_data=None になった行は skip される。"""
        from app.services import storage as storage_module
        from app.services import backup_restore as br_module
        backend = type("S", (), {
            "put": lambda self, k, p, m: None,
            "delete": lambda self, k: None,
            "get": lambda self, k: None,
        })()
        monkeypatch.setattr(storage_module, "get_storage_backend", lambda: backend)
        monkeypatch.setattr(br_module, "get_storage_backend", lambda: backend)

        backup_skeleton["data"]["vouchers"] = [
            {"id": 1, "image_key": "lost.jpg", "image_mime": "image/jpeg",
             "image_data": None, "_imageError": "lost"},
        ]
        resp = client.post(
            "/api/v1/backup/restore",
            headers=auth_header, json=backup_skeleton,
        )
        assert resp.status_code == 200
        assert resp.get_json()["restored"]["tables"]["vouchers"] == 0
        assert resp.get_json()["restored"]["storage"]["vouchers"] == 0


class TestBackupRestoreHelpers:
    """`_validate_backup` / `_b64_decode` / `_parse_date` / `_parse_datetime`
    の internal helper を直接呼んでカバレッジ補強。"""

    def test_validate_non_dict_backup(self):
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        with pytest.raises(BackupValidationError, match="object"):
            _validate_backup(1, "not a dict")

    def test_validate_account_in_accounts_must_be_dict(self):
        """accounts[*] が非 dict だと早期 isinstance フィルタで弾く。
        ↓ FK 検算 ループで `if not isinstance(row, dict)` 分岐を踏む。"""
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        b = {
            "version": "1.0", "user_id": 1,
            "data": {"journal_entry_lines": ["not a dict"]},
        }
        with pytest.raises(BackupValidationError, match="object"):
            _validate_backup(1, b)

    def test_validate_voucher_entry_id_not_dict(self):
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        b = {
            "version": "1.0", "user_id": 1,
            "data": {"vouchers": ["not a dict"]},
        }
        with pytest.raises(BackupValidationError, match="object"):
            _validate_backup(1, b)

    def test_validate_bcb_non_dict(self):
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        b = {
            "version": "1.0", "user_id": 1,
            "data": {"balance_cache_blobs": ["nope"]},
        }
        with pytest.raises(BackupValidationError, match="object"):
            _validate_backup(1, b)

    def test_validate_journal_entry_non_dict(self):
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        b = {
            "version": "1.0", "user_id": 1,
            "data": {"journal_entries": ["nope"]},
        }
        with pytest.raises(BackupValidationError, match="object"):
            _validate_backup(1, b)

    def test_validate_invalid_amount(self):
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        eb = base64.b64encode(b"\x42" * 48).decode("ascii")
        iv = base64.b64encode(b"\x42" * 12).decode("ascii")
        b = {
            "version": "1.0", "user_id": 1,
            "data": {
                "accounts": [{"code": "1010"}],
                "journal_entries": [
                    {"id": 1, "encrypted_blob": eb, "blob_iv": iv},
                ],
                "journal_entry_lines": [
                    {"journal_entry_id": 1, "account_code": "1010",
                     "debit_amount": "abc", "credit_amount": 0,
                     "encrypted_blob": eb, "blob_iv": iv},
                ],
            },
        }
        with pytest.raises(BackupValidationError, match="invalid amount"):
            _validate_backup(1, b)

    def _e2ee_voucher_backup(self, **overrides):
        """encrypted_meta_blob を持つ最小の E2EE 証憑入り backup を組む。"""
        meta = base64.b64encode(b"\xaa" * 32).decode()
        row = {
            "id": 1, "journal_entry_id": None,
            "image_data": base64.b64encode(b"\x00" * 40).decode(),
            "encrypted_meta_blob": meta,
            "meta_iv": base64.b64encode(b"\xbb" * 12).decode(),
            "aad_id": "12345",
            "file_size": 40,
        }
        row.update(overrides)
        return {
            "version": "1.0", "user_id": 1,
            "data": {"vouchers": [row]},
        }

    def test_validate_e2ee_voucher_ok(self):
        """正常な E2EE 証憑メタは検証を通過する。"""
        from app.services.backup_restore import _validate_backup
        _validate_backup(1, self._e2ee_voucher_backup())  # 例外が出ないこと

    def test_validate_voucher_aad_id_non_numeric(self):
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        with pytest.raises(BackupValidationError, match="aad_id"):
            _validate_backup(1, self._e2ee_voucher_backup(aad_id="not-a-number"))

    def test_validate_voucher_aad_id_out_of_range(self):
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        with pytest.raises(BackupValidationError, match="BigInteger 範囲外"):
            _validate_backup(1, self._e2ee_voucher_backup(aad_id=str(2 ** 63)))

    def test_validate_voucher_meta_iv_wrong_length(self):
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        bad_iv = base64.b64encode(b"\xbb" * 16).decode()  # 16 != 12
        with pytest.raises(BackupValidationError, match="meta_iv"):
            _validate_backup(1, self._e2ee_voucher_backup(meta_iv=bad_iv))

    def test_validate_voucher_aad_id_required(self):
        """E2EE 証憑 (encrypted_meta_blob あり) で aad_id=null は 400。"""
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        with pytest.raises(BackupValidationError, match="aad_id は E2EE 証憑で必須"):
            _validate_backup(1, self._e2ee_voucher_backup(aad_id=None))

    def test_validate_voucher_image_error_rejected(self):
        """export 時に画像取得失敗 (_imageError) した E2EE 行は 400。"""
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        b = self._e2ee_voucher_backup()
        b["data"]["vouchers"][0]["_imageError"] = "storage returned None"
        with pytest.raises(BackupValidationError, match="_imageError"):
            _validate_backup(1, b)

    def test_validate_voucher_thumbnail_error_rejected(self):
        """export 時にサムネ取得失敗 (_thumbnailError) した E2EE 行は 400。"""
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        b = self._e2ee_voucher_backup()
        b["data"]["vouchers"][0]["_thumbnailError"] = "IOError: disk gone"
        with pytest.raises(BackupValidationError, match="_thumbnailError"):
            _validate_backup(1, b)

    def test_validate_voucher_meta_iv_required(self):
        """E2EE 証憑 (encrypted_meta_blob あり) で meta_iv 欠落は 400。"""
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        b = self._e2ee_voucher_backup()
        del b["data"]["vouchers"][0]["meta_iv"]
        with pytest.raises(BackupValidationError, match="meta_iv は E2EE 証憑で必須"):
            _validate_backup(1, b)

    def test_validate_voucher_file_size_negative(self):
        from app.services.backup_restore import (
            _validate_backup, BackupValidationError,
        )
        with pytest.raises(BackupValidationError, match="file_size"):
            _validate_backup(1, self._e2ee_voucher_backup(file_size=-1))

    def test_validate_plaintext_voucher_skips_e2ee_checks(self):
        """encrypted_meta_blob なし (平文証憑) は E2EE フィールド検証の対象外。"""
        from app.services.backup_restore import _validate_backup
        b = {
            "version": "1.0", "user_id": 1,
            "data": {"vouchers": [{
                "id": 1, "image_data": base64.b64encode(b"x").decode(),
                "image_mime": "image/jpeg",
                # aad_id 等が無くても平文証憑なら通る
            }]},
        }
        _validate_backup(1, b)  # 例外が出ないこと

    def test_b64_decode_helpers(self):
        from app.services.backup_restore import (
            _b64_decode, BackupValidationError,
        )
        assert _b64_decode(None) is None
        assert _b64_decode(base64.b64encode(b"ok").decode()) == b"ok"
        with pytest.raises(BackupValidationError, match="string"):
            _b64_decode(123)
        with pytest.raises(BackupValidationError, match="invalid base64"):
            _b64_decode("!!!not base64!!!")

    def test_parse_date_helpers(self):
        from app.services.backup_restore import (
            _parse_date, BackupValidationError,
        )
        from datetime import date
        assert _parse_date(None) is None
        d = date(2026, 1, 1)
        assert _parse_date(d) == d
        assert _parse_date("2026-03-15") == date(2026, 3, 15)
        with pytest.raises(BackupValidationError, match="ISO string"):
            _parse_date(123)
        with pytest.raises(BackupValidationError, match="invalid date"):
            _parse_date("not a date")

    def test_parse_datetime_helpers(self):
        from app.services.backup_restore import _parse_datetime
        from datetime import datetime, timezone
        assert _parse_datetime(None) is None
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert _parse_datetime(dt) == dt
        assert _parse_datetime("2026-01-01T00:00:00") == datetime(2026, 1, 1)
        # 不正な値は silently None (例外なし)
        assert _parse_datetime(123) is None
        assert _parse_datetime("bogus") is None

    def test_cleanup_storage_swallows_errors(self):
        """_cleanup_storage は backend.delete の例外を吸収して続行する。"""
        from app.services.backup_restore import _cleanup_storage

        deleted = []

        class FlakyBackend:
            def delete(self, k):
                deleted.append(k)
                if "boom" in k:
                    raise IOError("disk gone")

        backend = FlakyBackend()
        # 例外を出さずに全 keys を試みること
        _cleanup_storage(backend, ["ok-1.jpg", "boom-2.jpg", "ok-3.jpg"])
        assert deleted == ["ok-1.jpg", "boom-2.jpg", "ok-3.jpg"]


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
