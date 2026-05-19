"""Phase 5 #70: AIDraft quota 統合 + Voucher 削除エンドポイント."""

import json
from datetime import date as date_type
from io import BytesIO
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.storage import StorageUsage
from app.models.user import User
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog


MB = 1024 * 1024


@pytest.fixture
def reset_limiter(app):
    """Rate limiter 状態をリセット (vouchers.attach/delete は 10/minute)."""
    try:
        from app.extensions import limiter
        limiter.reset()
    except Exception:
        pass
    yield


@pytest.fixture
def mock_storage(monkeypatch):
    """ストレージ backend と AI 解析を mock。"""
    from app.services import storage as storage_module
    from app.views import ai_journal as ai_journal_module
    from app.views import api as api_module
    from app.views import vouchers as vouchers_module
    from app.services import voucher as voucher_service

    stored = {}
    deleted = []

    def fake_store_image_with_thumbnail(key, image_bytes, mime_type):
        stored[key] = image_bytes
        stored[key + ".thumb"] = b"thumb"

    class FakeBackend:
        def delete(self, key):
            deleted.append(key)

        def get(self, key):
            return stored.get(key, b"")

        def put(self, key, data, content_type=None):
            stored[key] = data

    backend = FakeBackend()

    monkeypatch.setattr(storage_module, "store_image_with_thumbnail",
                        fake_store_image_with_thumbnail)
    monkeypatch.setattr(ai_journal_module, "store_image_with_thumbnail",
                        fake_store_image_with_thumbnail)
    monkeypatch.setattr(api_module, "store_image_with_thumbnail",
                        fake_store_image_with_thumbnail)
    monkeypatch.setattr(storage_module, "get_storage_backend",
                        lambda: backend)
    monkeypatch.setattr(ai_journal_module, "get_storage_backend",
                        lambda: backend)
    monkeypatch.setattr(api_module, "get_storage_backend",
                        lambda: backend)
    monkeypatch.setattr(vouchers_module, "get_storage_backend",
                        lambda: backend)
    monkeypatch.setattr(voucher_service, "get_storage_backend",
                        lambda: backend)

    return {"stored": stored, "deleted": deleted, "backend": backend}


def _make_draft(db, user, *, file_size=10 * MB, status="analyzed"):
    """テスト用 AIDraft を直接 DB に投入し、file_size 分を StorageUsage に計上."""
    draft = AIDraft(
        user_id=user.id,
        image_key=f"vouchers/{user.id}/draft.jpg",
        image_mime="image/jpeg",
        file_hash="a" * 64,
        file_size=file_size,
        suggestions_json=json.dumps([{
            "title": "テスト", "date": "2026-05-01",
            "entry_description": "テスト摘要", "lines": [],
        }]),
        status=status,
    )
    db.session.add(draft)
    usage = db.session.get(StorageUsage, user.id)
    if usage is None:
        db.session.add(StorageUsage(user_id=user.id, used_bytes=file_size))
    else:
        usage.used_bytes += file_size
    db.session.commit()
    return draft


class TestCreateVoucherFromDraft:
    """AIDraft → Voucher 所有権移転で file_size 引き継ぎ、計上不変."""

    def test_file_size_inherited(self, db, user):
        from app.services.voucher import create_voucher_from_draft
        from app.models.journal import JournalEntry

        entry = JournalEntry(
            user_id=user.id, date=date_type(2026, 5, 1),
            entry_number=1, description="テスト",
        )
        db.session.add(entry)
        db.session.flush()
        draft = _make_draft(db, user, file_size=5 * MB)

        voucher = create_voucher_from_draft(draft, entry.id)
        db.session.commit()

        assert voucher.file_size == 5 * MB
        assert voucher.image_key == draft.image_key
        assert voucher.file_hash == draft.file_hash

    def test_storage_usage_unchanged(self, db, user):
        """所有権移転なので StorageUsage は変動しない."""
        from app.services.voucher import create_voucher_from_draft
        from app.models.journal import JournalEntry

        entry = JournalEntry(
            user_id=user.id, date=date_type(2026, 5, 1),
            entry_number=1, description="テスト",
        )
        db.session.add(entry)
        db.session.flush()
        draft = _make_draft(db, user, file_size=7 * MB)
        before = db.session.get(StorageUsage, user.id).used_bytes

        create_voucher_from_draft(draft, entry.id)
        db.session.commit()

        after = db.session.get(StorageUsage, user.id).used_bytes
        assert after == before == 7 * MB

    def test_null_file_size_legacy(self, db, user):
        """file_size NULL のレガシー AIDraft でもクラッシュしない."""
        from app.services.voucher import create_voucher_from_draft
        from app.models.journal import JournalEntry

        entry = JournalEntry(
            user_id=user.id, date=date_type(2026, 5, 1),
            entry_number=1, description="テスト",
        )
        db.session.add(entry)
        db.session.flush()
        draft = AIDraft(
            user_id=user.id, image_key="legacy.jpg",
            image_mime="image/jpeg", file_hash="b" * 64,
            file_size=None, status="analyzed",
        )
        db.session.add(draft)
        db.session.commit()

        voucher = create_voucher_from_draft(draft, entry.id)
        db.session.commit()

        assert voucher.file_size is None


class TestAIJournalAnalyzeQuota:
    """ai_journal.analyze で AIDraft 生成時に容量計上."""

    def test_analyze_records_usage(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
    ):
        # AIConfig 必須
        db.session.add(UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_encrypted=b"dummy", model_name="gpt-4o",
        ))
        db.session.commit()

        from app.services.ai_receipt import JournalSuggestion

        fake_sugg = JournalSuggestion(
            title="テスト", description="", date="2026-05-01",
            entry_description="テスト", lines=[], compliance=None,
        )
        with patch("app.views.ai_journal.analyze_and_suggest",
                   return_value=[fake_sugg]):
            resp = logged_in_client.post(
                "/ai-journal/analyze",
                data={
                    "image_file": (BytesIO(b"x" * 100), "test.jpg", "image/jpeg"),
                    "comment": "テストコメント",
                },
                content_type="multipart/form-data",
            )

        assert resp.status_code == 200
        # AIDraft が file_size 付きで作成されている
        drafts = AIDraft.query.filter_by(user_id=user.id).all()
        assert len(drafts) == 1
        assert drafts[0].file_size == 100
        # StorageUsage に 100 bytes 計上されている
        usage = db.session.get(StorageUsage, user.id)
        assert usage.used_bytes == 100

    def test_analyze_quota_exceeded_returns_413(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
    ):
        """quota 上限直前まで使った状態で大きなアップロードを拒否."""
        db.session.add(UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_encrypted=b"dummy", model_name="gpt-4o",
        ))
        # quota 500MB のうち 499MB 使用済 → +2MB は超過
        db.session.add(StorageUsage(user_id=user.id, used_bytes=499 * MB))
        db.session.commit()

        big = b"x" * (2 * MB)
        resp = logged_in_client.post(
            "/ai-journal/analyze",
            data={
                "image_file": (BytesIO(big), "test.jpg", "image/jpeg"),
            },
            content_type="multipart/form-data",
        )

        assert resp.status_code == 413
        # AIDraft は作られない、StorageUsage も変動なし
        assert AIDraft.query.filter_by(user_id=user.id).count() == 0
        usage = db.session.get(StorageUsage, user.id)
        assert usage.used_bytes == 499 * MB

    def test_record_upload_failure_does_not_record_delete(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
        monkeypatch,
    ):
        """record_upload が例外を投げた場合、TOCTOU 再検証スキップで
        record_delete が呼ばれない (PR #93 review Finding 1 — 別ユーザー
        計上分の誤減算を防ぐ)."""
        db.session.add(UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_encrypted=b"dummy", model_name="gpt-4o",
        ))
        # 既に上限近くまで埋まっている状態 (495MB) を作る
        db.session.add(StorageUsage(user_id=user.id, used_bytes=495 * MB))
        db.session.commit()

        from app.services.ai_receipt import JournalSuggestion
        from app.views import ai_journal as ai_journal_module

        fake_sugg = JournalSuggestion(
            title="テスト", description="", date="2026-05-01",
            entry_description="テスト", lines=[], compliance=None,
        )

        # record_upload を必ず失敗させる
        def fake_record_upload(*args, **kwargs):
            raise RuntimeError("DB connection lost")

        record_delete_called = []

        def fake_record_delete(*args, **kwargs):
            record_delete_called.append(args)

        monkeypatch.setattr(ai_journal_module, "record_upload",
                            fake_record_upload)
        monkeypatch.setattr(ai_journal_module, "record_delete",
                            fake_record_delete)

        with patch("app.views.ai_journal.analyze_and_suggest",
                   return_value=[fake_sugg]):
            resp = logged_in_client.post(
                "/ai-journal/analyze",
                data={
                    "image_file": (BytesIO(b"x" * 1000), "test.jpg",
                                   "image/jpeg"),
                },
                content_type="multipart/form-data",
            )

        # 通常レスポンス (200) で TOCTOU の超過判定もスキップ
        assert resp.status_code == 200
        # record_delete は呼ばれていない (誤減算なし)
        assert record_delete_called == []
        # StorageUsage は別途加算されていない (record_upload 失敗のため) が、
        # 既存の他ユーザー分相当 495MB はそのまま残る
        usage = db.session.get(StorageUsage, user.id)
        assert usage.used_bytes == 495 * MB


class TestAIJournalUploadCleanup:
    """ai_journal.upload (GET) の temp drafts クリーンアップで record_delete."""

    def test_temp_drafts_cleanup_releases_quota(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
    ):
        # status="temp" の draft を 2 つ仕込む
        _make_draft(db, user, file_size=3 * MB, status="temp")
        _make_draft(db, user, file_size=5 * MB, status="temp")
        before = db.session.get(StorageUsage, user.id).used_bytes
        assert before == 8 * MB

        resp = logged_in_client.get("/ai-journal/")
        assert resp.status_code == 200

        # temp drafts は全削除、StorageUsage も 0 に減算
        assert AIDraft.query.filter_by(
            user_id=user.id, status="temp",
        ).count() == 0
        after = db.session.get(StorageUsage, user.id).used_bytes
        assert after == 0


class TestAIJournalDraftsDelete:
    """ai_journal.drafts_delete で record_delete."""

    def test_draft_delete_releases_quota(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
    ):
        draft = _make_draft(db, user, file_size=4 * MB)
        before = db.session.get(StorageUsage, user.id).used_bytes
        assert before == 4 * MB

        resp = logged_in_client.post(
            f"/ai-journal/drafts/{draft.id}/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 302

        assert db.session.get(AIDraft, draft.id) is None
        after = db.session.get(StorageUsage, user.id).used_bytes
        assert after == 0

    def test_draft_delete_null_file_size_skipped(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
    ):
        """file_size NULL のレガシー AIDraft 削除は減算をスキップ."""
        draft = AIDraft(
            user_id=user.id, image_key="legacy.jpg",
            image_mime="image/jpeg", file_size=None, status="analyzed",
        )
        db.session.add(draft)
        # 別途 10MB の計上を残しておく
        db.session.add(StorageUsage(user_id=user.id, used_bytes=10 * MB))
        db.session.commit()

        resp = logged_in_client.post(
            f"/ai-journal/drafts/{draft.id}/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        # NULL は減算対象外なので 10MB のまま
        assert db.session.get(StorageUsage, user.id).used_bytes == 10 * MB


class TestVoucherDelete:
    """Voucher 削除エンドポイント."""

    def _make_voucher(self, db, user, *, file_size=2 * MB):
        from app.models.journal import JournalEntry
        entry = JournalEntry(
            user_id=user.id, date=date_type(2026, 5, 1),
            entry_number=1, description="テスト",
        )
        db.session.add(entry)
        db.session.flush()
        voucher = Voucher(
            user_id=user.id, journal_entry_id=entry.id,
            image_key=f"vouchers/{user.id}/v.jpg",
            image_mime="image/jpeg",
            file_hash="c" * 64, file_size=file_size,
        )
        db.session.add(voucher)
        usage = db.session.get(StorageUsage, user.id)
        if usage is None:
            db.session.add(StorageUsage(user_id=user.id, used_bytes=file_size))
        else:
            usage.used_bytes += file_size
        db.session.commit()
        return voucher

    def test_delete_releases_quota(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
    ):
        voucher = self._make_voucher(db, user, file_size=3 * MB)
        assert db.session.get(StorageUsage, user.id).used_bytes == 3 * MB

        resp = logged_in_client.post(
            f"/vouchers/{voucher.id}/delete", follow_redirects=False,
        )
        assert resp.status_code == 302

        # Voucher は論理削除 (row は残るが deleted_at がセットされる)
        deleted = db.session.get(Voucher, voucher.id)
        assert deleted is not None
        assert deleted.deleted_at is not None
        # active() スコープからは除外される
        assert Voucher.active().filter_by(id=voucher.id).first() is None
        # StorageUsage は解放
        assert db.session.get(StorageUsage, user.id).used_bytes == 0
        # ストレージ削除呼び出し済
        assert any(
            "vouchers/" in k for k in mock_storage["deleted"]
        )

    def test_delete_other_user_voucher_404(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
    ):
        """他人の Voucher は 404."""
        other = User(
            username="other", email="other@example.com",
            user_type="personal",
        )
        other.set_password("x")
        db.session.add(other)
        db.session.commit()
        voucher = self._make_voucher(db, other, file_size=1 * MB)

        resp = logged_in_client.post(
            f"/vouchers/{voucher.id}/delete", follow_redirects=False,
        )
        assert resp.status_code == 404
        # 削除されていない
        assert db.session.get(Voucher, voucher.id) is not None

    def test_delete_blocked_during_proxy_view(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
    ):
        """代理閲覧中 (acting_as_user_id セッション) は削除禁止."""
        voucher = self._make_voucher(db, user, file_size=2 * MB)

        with logged_in_client.session_transaction() as sess:
            sess["acting_as_user_id"] = 999  # 何かしらの id

        resp = logged_in_client.post(
            f"/vouchers/{voucher.id}/delete", follow_redirects=False,
        )
        # acting_as_user_id がセットされている = 代理閲覧モードなので
        # delete はリダイレクトされる (redirect は 302)
        assert resp.status_code == 302
        # Voucher は残っている
        assert db.session.get(Voucher, voucher.id) is not None
        # StorageUsage も解放されない
        assert db.session.get(StorageUsage, user.id).used_bytes == 2 * MB

    def test_delete_persists_audit_log(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
    ):
        """Voucher 削除時、`action="deleted"` の AuditLog が永続化される
        (電帳法スキャナ保存「訂正削除の事実と内容を確認できること」要件)."""
        voucher = self._make_voucher(db, user, file_size=1 * MB)
        db.session.add(VoucherAuditLog(
            voucher_id=voucher.id, user_id=user.id, action="attached",
        ))
        db.session.commit()

        resp = logged_in_client.post(
            f"/vouchers/{voucher.id}/delete", follow_redirects=False,
        )
        assert resp.status_code == 302
        # attached + deleted の 2 件が残る (物理削除はしない)
        logs = VoucherAuditLog.query.filter_by(
            voucher_id=voucher.id,
        ).order_by(VoucherAuditLog.id).all()
        assert len(logs) == 2
        assert logs[0].action == "attached"
        assert logs[1].action == "deleted"
        # detail に image_key / file_hash / file_size が記録されている
        import json as _json
        deleted_detail = _json.loads(logs[1].detail or "{}")
        assert "image_key" in deleted_detail
        assert "file_hash" in deleted_detail
        assert deleted_detail.get("file_size") == 1 * MB

    def test_delete_null_file_size_skipped(
        self, logged_in_client, db, user, app, mock_storage, reset_limiter,
    ):
        """file_size NULL のレガシー Voucher 削除は StorageUsage 減算をスキップ."""
        from app.models.journal import JournalEntry
        entry = JournalEntry(
            user_id=user.id, date=date_type(2026, 5, 2),
            entry_number=2, description="テスト",
        )
        db.session.add(entry)
        db.session.flush()
        voucher = Voucher(
            user_id=user.id, journal_entry_id=entry.id,
            image_key="legacy.jpg", image_mime="image/jpeg",
            file_size=None,
        )
        db.session.add(voucher)
        db.session.add(StorageUsage(user_id=user.id, used_bytes=20 * MB))
        db.session.commit()

        resp = logged_in_client.post(
            f"/vouchers/{voucher.id}/delete", follow_redirects=False,
        )
        assert resp.status_code == 302
        # 20MB のまま
        assert db.session.get(StorageUsage, user.id).used_bytes == 20 * MB
