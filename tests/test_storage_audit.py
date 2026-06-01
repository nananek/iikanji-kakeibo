"""Phase 5 #70: ストレージ整合性監査バッチ."""

from datetime import date as date_type

import pytest

from app.models.ai_draft import AIDraft
from app.models.journal import JournalEntry
from app.models.storage import StorageUsage
from app.models.voucher import Voucher
from app.services.storage_audit import (
    audit_storage_usage,
    backfill_file_sizes,
    measure_user_usage,
)


MB = 1024 * 1024


@pytest.fixture
def mock_backend(monkeypatch):
    """ストレージ backend を mock し、key → bytes の辞書で動かす."""
    from app.services import storage_audit as audit_module

    files = {}

    class FakeBackend:
        def get(self, key):
            if key not in files:
                raise FileNotFoundError(key)
            return files[key]

    backend = FakeBackend()
    monkeypatch.setattr(audit_module, "get_storage_backend", lambda: backend)
    return files


class TestBackfillFileSizes:
    def test_backfills_null_voucher(self, db, user, mock_backend):
        entry = JournalEntry(
            user_id=user.id,
            entry_number=1,
        )
        db.session.add(entry)
        db.session.flush()
        voucher = Voucher(
            user_id=user.id, journal_entry_id=entry.id,
            image_key="v1.jpg",
            file_size=None,
        )
        db.session.add(voucher)
        db.session.commit()
        mock_backend["v1.jpg"] = b"x" * 1234

        stats = backfill_file_sizes()

        assert stats["voucher_backfilled"] == 1
        assert stats["draft_backfilled"] == 0
        assert stats["errors"] == []
        assert db.session.get(Voucher, voucher.id).file_size == 1234

    def test_backfills_null_draft(self, db, user, mock_backend):
        draft = AIDraft(
            user_id=user.id, image_key="d1.jpg",
            image_mime="image/jpeg", file_size=None,
        )
        db.session.add(draft)
        db.session.commit()
        mock_backend["d1.jpg"] = b"y" * 567

        stats = backfill_file_sizes()

        assert stats["voucher_backfilled"] == 0
        assert stats["draft_backfilled"] == 1
        assert db.session.get(AIDraft, draft.id).file_size == 567

    def test_skips_non_null(self, db, user, mock_backend):
        draft = AIDraft(
            user_id=user.id, image_key="d2.jpg",
            image_mime="image/jpeg", file_size=999,
        )
        db.session.add(draft)
        db.session.commit()
        mock_backend["d2.jpg"] = b"z" * 100

        stats = backfill_file_sizes()
        assert stats["draft_backfilled"] == 0
        # 値は変動しない
        assert db.session.get(AIDraft, draft.id).file_size == 999

    def test_storage_missing_recorded_as_error(self, db, user, mock_backend):
        draft = AIDraft(
            user_id=user.id, image_key="orphan.jpg",
            image_mime="image/jpeg", file_size=None,
        )
        db.session.add(draft)
        db.session.commit()
        # mock_backend に key を入れない → FileNotFoundError

        stats = backfill_file_sizes()
        assert stats["draft_backfilled"] == 0
        assert len(stats["errors"]) == 1
        assert "orphan.jpg" in stats["errors"][0]


class TestMeasureUserUsage:
    def test_sums_voucher_and_draft_file_sizes(self, db, user):
        entry = JournalEntry(
            user_id=user.id,
            entry_number=1,
        )
        db.session.add(entry)
        db.session.flush()
        db.session.add(Voucher(
            user_id=user.id, journal_entry_id=entry.id,
            image_key="v.jpg", file_size=3 * MB,
        ))
        db.session.add(AIDraft(
            user_id=user.id, image_key="d.jpg",
            image_mime="image/jpeg", file_size=2 * MB,
        ))
        db.session.commit()

        assert measure_user_usage(user.id) == 5 * MB

    def test_returns_zero_when_no_records(self, db, user):
        assert measure_user_usage(user.id) == 0


class TestAuditStorageUsage:
    def test_no_drift_reported(self, db, user):
        # voucher 1 件, file_size=100、StorageUsage も 100
        entry = JournalEntry(
            user_id=user.id,
            entry_number=1,
        )
        db.session.add(entry)
        db.session.flush()
        db.session.add(Voucher(
            user_id=user.id, journal_entry_id=entry.id,
            image_key="v.jpg", file_size=100,
        ))
        db.session.add(StorageUsage(user_id=user.id, used_bytes=100))
        db.session.commit()

        stats = audit_storage_usage()
        assert stats["users_checked"] == 1
        assert stats["drift_detected"] == 0
        assert stats["drifts"] == []

    def test_detects_drift_without_fix(self, db, user):
        entry = JournalEntry(
            user_id=user.id,
            entry_number=1,
        )
        db.session.add(entry)
        db.session.flush()
        db.session.add(Voucher(
            user_id=user.id, journal_entry_id=entry.id,
            image_key="v.jpg", file_size=100,
        ))
        # StorageUsage は 50 (drift -50)
        db.session.add(StorageUsage(user_id=user.id, used_bytes=50))
        db.session.commit()

        stats = audit_storage_usage(fix=False)
        assert stats["drift_detected"] == 1
        assert stats["drift_fixed"] == 0
        assert stats["drifts"][0]["measured"] == 100
        assert stats["drifts"][0]["recorded"] == 50
        assert stats["drifts"][0]["delta"] == 50
        # fix=False なので StorageUsage は変更されない
        assert db.session.get(StorageUsage, user.id).used_bytes == 50

    def test_fixes_drift(self, db, user):
        entry = JournalEntry(
            user_id=user.id,
            entry_number=1,
        )
        db.session.add(entry)
        db.session.flush()
        db.session.add(Voucher(
            user_id=user.id, journal_entry_id=entry.id,
            image_key="v.jpg", file_size=100,
        ))
        db.session.add(StorageUsage(user_id=user.id, used_bytes=50))
        db.session.commit()

        stats = audit_storage_usage(fix=True)
        assert stats["drift_fixed"] == 1
        # fix=True で実測値 100 に同期
        assert db.session.get(StorageUsage, user.id).used_bytes == 100

    def test_creates_usage_row_when_missing(self, db, user):
        entry = JournalEntry(
            user_id=user.id,
            entry_number=1,
        )
        db.session.add(entry)
        db.session.flush()
        db.session.add(Voucher(
            user_id=user.id, journal_entry_id=entry.id,
            image_key="v.jpg", file_size=100,
        ))
        # StorageUsage row なし
        db.session.commit()

        stats = audit_storage_usage(fix=True)
        assert stats["drift_fixed"] == 1
        usage = db.session.get(StorageUsage, user.id)
        assert usage is not None
        assert usage.used_bytes == 100
