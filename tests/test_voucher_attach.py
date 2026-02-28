"""証憑添付テスト — 既存仕訳への証憑画像アップロード"""

import hashlib
import io
from unittest.mock import patch

import pytest

from app.extensions import db as _db
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.models.ai_config import UserAIConfig
from app.services.voucher import create_voucher_from_upload
from tests.conftest import make_journal

# 1x1 白 JPEG (最小限のテスト画像)
TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.\x27 \"*2;((.82telerik9-/444\xff\xc0"
    b"\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f"
    b"\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4"
    b"\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04"
    b"\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"
    b"\x22q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n"
    b"\x16\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz"
    b"\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98"
    b"\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5"
    b"\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2"
    b"\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7"
    b"\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda"
    b"\x00\x08\x01\x01\x00\x00?\x00\xfb\xd2\x8a+\xff\xd9"
)


class TestCreateVoucherFromUpload:
    """create_voucher_from_upload() のユニットテスト"""

    @patch("app.services.voucher.store_image_with_thumbnail")
    def test_creates_voucher_linked_to_entry(self, mock_store, db, user, accounts):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
        )
        voucher = create_voucher_from_upload(
            user_id=user.id,
            journal_entry_id=entry.id,
            image_bytes=TINY_JPEG,
            mime_type="image/jpeg",
            original_filename="receipt.jpg",
        )
        db.session.commit()

        assert voucher.id is not None
        assert voucher.journal_entry_id == entry.id
        assert voucher.user_id == user.id
        assert voucher.image_mime == "image/jpeg"
        assert voucher.original_filename == "receipt.jpg"
        assert voucher.image_key != ""
        mock_store.assert_called_once()

    @patch("app.services.voucher.store_image_with_thumbnail")
    def test_computes_sha256_hash(self, mock_store, db, user, accounts):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
        )
        voucher = create_voucher_from_upload(
            user_id=user.id,
            journal_entry_id=entry.id,
            image_bytes=TINY_JPEG,
            mime_type="image/jpeg",
        )
        db.session.commit()

        expected_hash = hashlib.sha256(TINY_JPEG).hexdigest()
        assert voucher.file_hash == expected_hash

    @patch("app.services.voucher.store_image_with_thumbnail")
    def test_creates_audit_log(self, mock_store, db, user, accounts):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 300,
        )
        voucher = create_voucher_from_upload(
            user_id=user.id,
            journal_entry_id=entry.id,
            image_bytes=TINY_JPEG,
            mime_type="image/jpeg",
        )
        db.session.commit()

        log = VoucherAuditLog.query.filter_by(
            voucher_id=voucher.id, action="attached",
        ).first()
        assert log is not None
        assert str(entry.id) in log.detail


class TestAttachEndpoint:
    """POST /vouchers/attach/<entry_id> のテスト"""

    @patch("app.services.voucher.store_image_with_thumbnail")
    def test_attach_success_no_ai(self, mock_store, logged_in_client, user, accounts, db):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 2000,
        )

        data = {"image": (io.BytesIO(TINY_JPEG), "receipt.jpg", "image/jpeg")}
        resp = logged_in_client.post(
            f"/vouchers/attach/{entry.id}",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        result = resp.get_json()
        assert result["ok"] is True
        assert "voucher_id" in result
        assert "compliance" not in result
        assert "consistency" not in result

    def test_attach_wrong_entry_404(self, logged_in_client, user):
        data = {"image": (io.BytesIO(TINY_JPEG), "receipt.jpg", "image/jpeg")}
        resp = logged_in_client.post(
            "/vouchers/attach/99999",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 404

    @patch("app.services.voucher.store_image_with_thumbnail")
    def test_attach_other_user_entry(
        self, mock_store, logged_in_client, second_user, account_types, db,
    ):
        """他ユーザーの仕訳には添付できない"""
        from tests.conftest import make_journal as mj
        from app.models.account import Account

        acct = Account(
            user_id=second_user.id, account_type_id=account_types["expense"].id,
            code="5010", name="食費", is_active=True,
        )
        acct2 = Account(
            user_id=second_user.id, account_type_id=account_types["asset"].id,
            code="1010", name="現金", is_active=True,
        )
        db.session.add_all([acct, acct2])
        db.session.commit()

        entry = mj(db, second_user.id, acct.id, acct2.id, 1000)

        data = {"image": (io.BytesIO(TINY_JPEG), "receipt.jpg", "image/jpeg")}
        resp = logged_in_client.post(
            f"/vouchers/attach/{entry.id}",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 404

    def test_attach_no_file_400(self, logged_in_client, user, accounts, db):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
        )
        resp = logged_in_client.post(
            f"/vouchers/attach/{entry.id}",
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_attach_invalid_mime_400(self, logged_in_client, user, accounts, db):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
        )
        data = {"image": (io.BytesIO(b"fake pdf"), "doc.pdf", "application/pdf")}
        resp = logged_in_client.post(
            f"/vouchers/attach/{entry.id}",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    @patch("app.services.voucher.store_image_with_thumbnail")
    @patch("app.services.ai_receipt.analyze_voucher_for_attachment")
    def test_attach_with_ai_returns_results(
        self, mock_analyze, mock_store, logged_in_client, user, accounts, db,
    ):
        """AI設定がある場合、compliance/consistency がレスポンスに含まれる"""
        from app.services.ai_receipt import encrypt_api_key

        config = UserAIConfig(
            user_id=user.id,
            provider="openai",
            api_key_encrypted=encrypt_api_key("sk-test"),
            model_name="gpt-4o",
        )
        db.session.add(config)
        db.session.commit()

        mock_analyze.return_value = {
            "compliance": {
                "status": "pass", "warnings": [], "details": [],
            },
            "consistency": {
                "status": "pass",
                "date_match": True,
                "amount_match": True,
                "description_match": True,
                "warnings": [],
            },
        }

        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1500,
        )
        data = {"image": (io.BytesIO(TINY_JPEG), "receipt.jpg", "image/jpeg")}
        resp = logged_in_client.post(
            f"/vouchers/attach/{entry.id}",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        result = resp.get_json()
        assert result["ok"] is True
        assert result["compliance"]["status"] == "pass"
        assert result["consistency"]["status"] == "pass"
        mock_analyze.assert_called_once()

    @patch("app.services.voucher.store_image_with_thumbnail")
    @patch("app.services.ai_receipt.analyze_voucher_for_attachment")
    def test_attach_ai_failure_no_block(
        self, mock_analyze, mock_store, logged_in_client, user, accounts, db,
    ):
        """AI解析が例外を出してもVoucherは作成される"""
        from app.services.ai_receipt import encrypt_api_key

        config = UserAIConfig(
            user_id=user.id,
            provider="openai",
            api_key_encrypted=encrypt_api_key("sk-test"),
            model_name="gpt-4o",
        )
        db.session.add(config)
        db.session.commit()

        mock_analyze.side_effect = RuntimeError("API error")

        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 800,
        )
        data = {"image": (io.BytesIO(TINY_JPEG), "receipt.jpg", "image/jpeg")}
        resp = logged_in_client.post(
            f"/vouchers/attach/{entry.id}",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        result = resp.get_json()
        assert result["ok"] is True
        assert "ai_error" in result

        # Voucher は作成されている
        voucher = Voucher.query.filter_by(id=result["voucher_id"]).first()
        assert voucher is not None
        assert voucher.journal_entry_id == entry.id
