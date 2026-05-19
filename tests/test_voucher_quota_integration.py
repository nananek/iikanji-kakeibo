"""Phase 5 #70: create_voucher_from_upload の quota 統合テスト."""

import pytest

from app.models.storage import StorageUsage
from app.models.voucher import Voucher
from app.services.entitlement import UnlimitedBillingClient
from app.services.storage_quota import QuotaExceededError
from app.services.voucher import create_voucher_from_upload


MB = 1024 * 1024


def _patch_entitlement(monkeypatch, *, has_voucher_storage: bool):
    from app.services import entitlement as ent

    class Client(UnlimitedBillingClient):
        def has_entitlement(self, user, feature_key):
            if feature_key == "voucher_storage":
                return has_voucher_storage
            return True

    monkeypatch.setattr(ent, "get_billing_client", lambda: Client())


def _make_entry(db, user, accounts):
    from tests.conftest import make_journal
    return make_journal(
        db, user.id, "1010", "5010", 1000,
    )


class TestCreateVoucherQuotaCheck:
    def test_rejects_without_voucher_storage(
        self, app, db, user, accounts, monkeypatch
    ):
        _patch_entitlement(monkeypatch, has_voucher_storage=False)
        entry = _make_entry(db, user, accounts)
        with app.app_context():
            with pytest.raises(QuotaExceededError, match="有償プラン"):
                create_voucher_from_upload(
                    user_id=user.id,
                    journal_entry_id=entry.id,
                    image_bytes=b"x" * 100,
                    mime_type="image/png",
                )
        # 巻き戻されているため Voucher 行は作成されない
        assert Voucher.query.count() == 0
        # StorageUsage も更新されない
        usage = db.session.get(StorageUsage, user.id)
        assert usage is None or usage.used_bytes == 0

    def test_records_size_on_success(
        self, app, db, user, accounts, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(app.config, "STORAGE_LOCAL_DIR", str(tmp_path))
        entry = _make_entry(db, user, accounts)
        with app.app_context():
            voucher = create_voucher_from_upload(
                user_id=user.id,
                journal_entry_id=entry.id,
                image_bytes=b"x" * 5000,
                mime_type="image/png",
            )
            db.session.commit()
            assert voucher.file_size == 5000
            usage = db.session.get(StorageUsage, user.id)
            assert usage.used_bytes == 5000

    def test_rejects_over_quota(
        self, app, db, user, accounts, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(app.config, "STORAGE_LOCAL_DIR", str(tmp_path))
        monkeypatch.setitem(app.config, "STORAGE_QUOTA_BYTES_DEFAULT", 1000)
        # 既に 900 bytes 使用済
        db.session.add(StorageUsage(user_id=user.id, used_bytes=900))
        db.session.commit()
        entry = _make_entry(db, user, accounts)

        with app.app_context():
            with pytest.raises(QuotaExceededError, match="容量上限"):
                create_voucher_from_upload(
                    user_id=user.id,
                    journal_entry_id=entry.id,
                    image_bytes=b"x" * 200,  # 900 + 200 > 1000
                    mime_type="image/png",
                )
        # 巻き戻されているため Voucher は作成されない
        assert Voucher.query.count() == 0
        # 残量も変わらない
        usage = db.session.get(StorageUsage, user.id)
        assert usage.used_bytes == 900


class TestAttachEndpointQuota:
    def test_attach_returns_413_when_quota_exceeded(
        self, db, logged_in_client, user, accounts, monkeypatch
    ):
        _patch_entitlement(monkeypatch, has_voucher_storage=False)
        entry = _make_entry(db, user, accounts)

        import io
        resp = logged_in_client.post(
            f"/vouchers/attach/{entry.id}",
            data={
                "image": (io.BytesIO(b"x" * 100), "test.png", "image/png"),
            },
            content_type="multipart/form-data",
        )
        assert resp.status_code == 413
        body = resp.get_json()
        assert "有償プラン" in body["error"]


class TestCreateVoucherFromDraftDocstring:
    """create_voucher_from_draft はまだ quota 統合してない旨を docstring で
    申し送り済 (本 PR ではテストのみで挙動は変えない)"""

    def test_docstring_mentions_phase5_followup(self):
        from app.services.voucher import create_voucher_from_draft
        assert "Phase 5" in (create_voucher_from_draft.__doc__ or "")
