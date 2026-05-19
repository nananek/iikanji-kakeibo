"""Phase 5 #70: 設定画面のストレージ使用量セクション."""

import pytest

from app.models.storage import StorageUsage
from app.services.storage_quota import get_storage_summary


MB = 1024 * 1024


def _patch_entitlement(monkeypatch, *, has_voucher_storage: bool):
    from app.services import entitlement as ent
    from app.services.entitlement import UnlimitedBillingClient

    class Client(UnlimitedBillingClient):
        def has_entitlement(self, user, feature_key):
            if feature_key == "voucher_storage":
                return has_voucher_storage
            return True

    monkeypatch.setattr(ent, "get_billing_client", lambda: Client())


class TestGetStorageSummary:
    """`get_storage_summary` のロジック検証"""

    def test_returns_none_when_no_voucher_storage(
        self, app, user, monkeypatch
    ):
        _patch_entitlement(monkeypatch, has_voucher_storage=False)
        with app.app_context():
            summary = get_storage_summary(user)
        assert summary is None

    def test_returns_zero_when_no_record(self, app, user):
        with app.app_context():
            summary = get_storage_summary(user)
        assert summary["used_bytes"] == 0
        assert summary["used_mb"] == 0.0
        assert summary["percentage"] == 0
        assert summary["level"] == "ok"

    def test_calculates_percentage(self, app, db, user):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=50 * MB))
        db.session.commit()
        with app.app_context():
            summary = get_storage_summary(user)
        # default quota 500 MB → 50/500 = 10%
        assert summary["percentage"] == 10.0
        assert summary["level"] == "ok"

    @pytest.mark.parametrize("used_mb, expected_level", [
        (50, "ok"),       # 10% → ok
        (399, "ok"),      # 79.8% → ok
        (400, "warning"), # 80.0% → warning
        (474, "warning"), # 94.8% → warning
        (475, "critical"),# 95.0% → critical
        (500, "critical"),# 100% → critical
    ])
    def test_level_thresholds(self, app, db, user, used_mb, expected_level):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=used_mb * MB))
        db.session.commit()
        with app.app_context():
            summary = get_storage_summary(user)
        assert summary["level"] == expected_level


class TestSettingsPageStorageSection:
    """設定画面に使用容量セクションが描画される"""

    def test_section_visible(self, logged_in_client, user, db):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=100 * MB))
        db.session.commit()
        resp = logged_in_client.get("/settings/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "ストレージ使用量" in body
        assert "100.0 MB" in body
        assert "500.0 MB" in body
        assert "progress" in body

    def test_warning_badge_at_80_percent(
        self, logged_in_client, user, db
    ):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=400 * MB))
        db.session.commit()
        resp = logged_in_client.get("/settings/")
        body = resp.get_data(as_text=True)
        assert "80% 以上使用中" in body
        assert "bg-warning" in body

    def test_critical_badge_at_95_percent(
        self, logged_in_client, user, db
    ):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=475 * MB))
        db.session.commit()
        resp = logged_in_client.get("/settings/")
        body = resp.get_data(as_text=True)
        assert "残量わずか" in body
        assert "bg-danger" in body

    def test_no_badge_when_under_80_percent(
        self, logged_in_client, user, db
    ):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=100 * MB))
        db.session.commit()
        resp = logged_in_client.get("/settings/")
        body = resp.get_data(as_text=True)
        assert "残量わずか" not in body
        assert "80% 以上使用中" not in body

    def test_section_hidden_for_non_voucher_storage_user(
        self, logged_in_client, user, monkeypatch
    ):
        """voucher_storage 未契約ユーザーには容量セクションが出ない"""
        _patch_entitlement(monkeypatch, has_voucher_storage=False)
        resp = logged_in_client.get("/settings/")
        body = resp.get_data(as_text=True)
        assert "ストレージ使用量" not in body
        assert "MB / " not in body  # 「X.X MB / Y.Y MB」表示が出ない

    def test_section_hidden_when_billing_unimplemented(
        self, logged_in_client, user, app, monkeypatch
    ):
        """BILLING_BACKEND=http 等の NotImplementedError でもセクション非表示で 500 にならない"""
        monkeypatch.setitem(app.config, "BILLING_BACKEND", "http")
        resp = logged_in_client.get("/settings/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "ストレージ使用量" not in body
