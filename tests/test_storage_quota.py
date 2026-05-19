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

    def test_error_has_user_message_attribute(
        self, app, db, user, monkeypatch,
    ):
        """`user_message` 属性に固定文言が格納される (CodeQL 誤検出対策)."""
        _patch_entitlement(monkeypatch, has_voucher_storage=False)
        with app.app_context():
            try:
                check_quota(user, incoming_size=1)
            except QuotaExceededError as exc:
                # view 側は `str(exc)` ではなく `exc.user_message` 経由で
                # ユーザー向け文言を返す。両者の文字列内容は一致するが、
                # data flow を分離して静的解析の誤検出を避ける。
                assert hasattr(exc, "user_message")
                assert "有償プラン" in exc.user_message
                assert exc.user_message == str(exc)
            else:
                pytest.fail("QuotaExceededError should have been raised")

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

    def test_no_op_when_no_record(self, app, db, user, caplog):
        import logging
        with app.app_context():
            with caplog.at_level(logging.WARNING):
                record_delete(user, size=100)
        assert db.session.get(StorageUsage, user.id) is None
        # 整合性異常検知のための warning ログ
        assert any("does not exist" in r.message for r in caplog.records)


class TestPositiveSizeValidation:
    """size / incoming_size <= 0 は ValueError"""

    @pytest.mark.parametrize("size", [0, -1, -100])
    def test_record_upload_rejects_non_positive(self, app, user, size):
        with app.app_context():
            with pytest.raises(ValueError, match="size must be positive"):
                record_upload(user, size=size)

    @pytest.mark.parametrize("size", [0, -1, -100])
    def test_record_delete_rejects_non_positive(self, app, user, size):
        with app.app_context():
            with pytest.raises(ValueError, match="size must be positive"):
                record_delete(user, size=size)

    @pytest.mark.parametrize("size", [0, -1, -100])
    def test_check_quota_rejects_non_positive(self, app, user, size):
        with app.app_context():
            with pytest.raises(
                ValueError, match="incoming_size must be positive"
            ):
                check_quota(user, incoming_size=size)


class TestUpdatedAtTracking:
    """Core UPDATE 経由でも `updated_at` が確実に更新される"""

    def test_record_upload_bumps_updated_at(self, app, db, user):
        import time
        from datetime import datetime
        # 古い updated_at で 1 行作る
        old_ts = datetime(2025, 1, 1)
        db.session.add(StorageUsage(
            user_id=user.id, used_bytes=100, updated_at=old_ts,
        ))
        db.session.commit()

        time.sleep(0.01)  # 確実に時刻が進むよう微小スリープ
        with app.app_context():
            record_upload(user, size=50)

        row = db.session.get(StorageUsage, user.id)
        assert row.used_bytes == 150
        # SQLite は DateTime(timezone=True) でも tz-naive で読み出すので、
        # 比較前に naive に揃える。
        actual = row.updated_at.replace(tzinfo=None) if row.updated_at.tzinfo else row.updated_at
        assert actual > old_ts

    def test_record_delete_bumps_updated_at(self, app, db, user):
        import time
        from datetime import datetime
        old_ts = datetime(2025, 1, 1)
        db.session.add(StorageUsage(
            user_id=user.id, used_bytes=500, updated_at=old_ts,
        ))
        db.session.commit()

        time.sleep(0.01)
        with app.app_context():
            record_delete(user, size=200)

        row = db.session.get(StorageUsage, user.id)
        assert row.used_bytes == 300
        # SQLite は DateTime(timezone=True) でも tz-naive で読み出すので、
        # 比較前に naive に揃える。
        actual = row.updated_at.replace(tzinfo=None) if row.updated_at.tzinfo else row.updated_at
        assert actual > old_ts


class TestConcurrentInsertFallback:
    """初回 INSERT が並行リクエストで競合した場合のフォールバック."""

    def test_integrity_error_falls_back_to_update(
        self, app, db, user, monkeypatch
    ):
        """`db.session.add` 直後の `flush` で IntegrityError を擬似発火
        させ、アトミック UPDATE への自動フォールバックを検証する。"""
        from sqlalchemy.exc import IntegrityError as SAIntegrityError

        # 既に StorageUsage がある状態を作る (= 別リクエストが先に INSERT 済)
        db.session.add(StorageUsage(user_id=user.id, used_bytes=100))
        db.session.commit()

        # storage_quota.record_upload を直接呼ぶと UPDATE rowcount=1 で
        # 通常パスに乗ってしまうので、最初の UPDATE を rowcount=0 で
        # 上書きする状況を再現する: storage_quota.update 関数を patch
        from app.services import storage_quota as sq

        real_execute = db.session.execute
        call_count = {"n": 0}

        def fake_execute(stmt, *args, **kwargs):
            # 最初の UPDATE 呼び出しのみ rowcount=0 のレスポンスを返す
            class FakeResult:
                rowcount = 0
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FakeResult()
            return real_execute(stmt, *args, **kwargs)

        monkeypatch.setattr(db.session, "execute", fake_execute)

        with app.app_context():
            sq.record_upload(user, size=50)

        # フォールバック UPDATE で +50 加算され、最終的に 150 になる
        # (擬似発火のため flow が複雑だが、最終状態が正しいことを確認)
        row = db.session.get(StorageUsage, user.id)
        assert row.used_bytes == 150


class TestAtomicUpdate:
    """アトミック UPDATE (read-modify-write の消失を防ぐ)"""

    def test_upload_increments_via_sql_update(self, app, db, user):
        """`db.session.get(...).used_bytes = X; commit()` ではなく
        SQL UPDATE で加算されるため、別セッションの値変更があっても
        消失しないことを示す簡易検証。"""
        # 事前にレコードあり
        db.session.add(StorageUsage(user_id=user.id, used_bytes=100))
        db.session.commit()

        with app.app_context():
            record_upload(user, size=50)

        # 加算後の値
        row = db.session.get(StorageUsage, user.id)
        assert row.used_bytes == 150

    def test_delete_decrements_via_sql_update(self, app, db, user):
        db.session.add(StorageUsage(user_id=user.id, used_bytes=500))
        db.session.commit()
        with app.app_context():
            record_delete(user, size=200)
        row = db.session.get(StorageUsage, user.id)
        assert row.used_bytes == 300
