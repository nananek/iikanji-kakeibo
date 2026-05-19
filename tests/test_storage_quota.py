"""Phase 5 #70: ストレージクオータの基盤テスト."""

import pytest

from app.models.storage import StorageUsage
from app.services.storage_quota import (
    QuotaExceededError,
    check_quota,
    get_quota_bytes,
    get_used_bytes,
    record_delete,
    record_upload,
)


MB = 1024 * 1024


class TestStorageUsageModel:
    def test_default_used_bytes_is_zero(self, db, user):
        row = StorageUsage(user_id=user.id)
        db.session.add(row)
        db.session.commit()
        fetched = db.session.get(StorageUsage, user.id)
        assert fetched.used_bytes == 0


class TestGetUsedBytes:
    def test_returns_zero_when_no_record(self, app, db, user):
        with app.app_context():
            assert get_used_bytes(user) == 0

    def test_returns_recorded_value(self, app, db, user):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=123))
        db.session.commit()
        with app.app_context():
            assert get_used_bytes(user) == 123


class TestGetQuotaBytes:
    def test_default_is_500mb(self, app, user):
        with app.app_context():
            assert get_quota_bytes(user) == 500 * MB

    def test_respects_config_override(self, app, user, monkeypatch):
        monkeypatch.setitem(app.config, "STORAGE_QUOTA_BYTES_DEFAULT", 1024)
        with app.app_context():
            assert get_quota_bytes(user) == 1024


def _patch_entitlement(monkeypatch, *, has_voucher_storage: bool):
    from app.services import entitlement as ent
    from app.services.entitlement import UnlimitedBillingClient

    class Client(UnlimitedBillingClient):
        def has_entitlement(self, user, feature_key):
            if feature_key == "voucher_storage":
                return has_voucher_storage
            return True

    monkeypatch.setattr(ent, "get_billing_client", lambda: Client())


class TestCheckQuota:
    def test_rejects_without_voucher_storage(self, app, db, user, monkeypatch):
        _patch_entitlement(monkeypatch, has_voucher_storage=False)
        with app.app_context():
            with pytest.raises(QuotaExceededError, match="有償プラン"):
                check_quota(user, incoming_size=1)

    def test_passes_under_quota(self, app, db, user, monkeypatch):
        _patch_entitlement(monkeypatch, has_voucher_storage=True)
        db.session.add(StorageUsage(user_id=user.id, used_bytes=100 * MB))
        db.session.commit()
        with app.app_context():
            # 残り 400 MB あるので 100 MB アップロードは OK
            check_quota(user, incoming_size=100 * MB)

    def test_passes_exactly_at_quota(self, app, db, user, monkeypatch):
        _patch_entitlement(monkeypatch, has_voucher_storage=True)
        db.session.add(StorageUsage(user_id=user.id, used_bytes=0))
        db.session.commit()
        with app.app_context():
            # used + incoming == quota の境界は OK
            check_quota(user, incoming_size=500 * MB)

    def test_rejects_over_quota(self, app, db, user, monkeypatch):
        _patch_entitlement(monkeypatch, has_voucher_storage=True)
        db.session.add(StorageUsage(user_id=user.id, used_bytes=500 * MB))
        db.session.commit()
        with app.app_context():
            with pytest.raises(QuotaExceededError, match="容量上限"):
                check_quota(user, incoming_size=1)

    def test_default_unlimited_passes(self, app, db, user):
        """セルフホスト (UnlimitedBillingClient デフォルト) では voucher_storage
        も True で扱われ、上限内なら通過する。"""
        with app.app_context():
            check_quota(user, incoming_size=10 * MB)


class TestRecordUpload:
    def test_creates_new_row(self, app, db, user):
        with app.app_context():
            record_upload(user, size=12345)
        row = db.session.get(StorageUsage, user.id)
        assert row.used_bytes == 12345

    def test_adds_to_existing_row(self, app, db, user):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=1000))
        db.session.commit()
        with app.app_context():
            record_upload(user, size=500)
        row = db.session.get(StorageUsage, user.id)
        assert row.used_bytes == 1500


class TestRecordDelete:
    def test_subtracts_from_existing(self, app, db, user):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=1000))
        db.session.commit()
        with app.app_context():
            record_delete(user, size=300)
        row = db.session.get(StorageUsage, user.id)
        assert row.used_bytes == 700

    def test_does_not_go_below_zero(self, app, db, user):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=100))
        db.session.commit()
        with app.app_context():
            record_delete(user, size=500)
        row = db.session.get(StorageUsage, user.id)
        assert row.used_bytes == 0

    def test_no_op_when_no_record(self, app, db, user):
        with app.app_context():
            record_delete(user, size=100)
        assert db.session.get(StorageUsage, user.id) is None
