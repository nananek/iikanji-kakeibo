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


class TestTOCTOURollback:
    """`record_upload` 後の楽観的再検証で巻き戻しが正しく動作する"""

    def test_rollback_when_concurrent_upload_pushes_over_quota(
        self, app, db, user, accounts, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(app.config, "STORAGE_LOCAL_DIR", str(tmp_path))
        monkeypatch.setitem(app.config, "STORAGE_QUOTA_BYTES_DEFAULT", 10000)
        entry = _make_entry(db, user, accounts)

        # `record_upload` の中身を「加算後にさらに別リクエストが容量を埋めた」
        # 状況に差し替える。`real_record_upload` で加算 → 直後に上限直上まで
        # 書き換えることで TOCTOU 競合をシミュレートする。
        from app.models.storage import StorageUsage
        from app.services import voucher as voucher_mod
        from app.services.storage_quota import (
            record_upload as real_record_upload,
        )

        def fake_record_upload(u, sz):
            real_record_upload(u, sz)
            row = db.session.get(StorageUsage, u.id)
            row.used_bytes = 10000 + 1  # 必ず上限を 1 byte 超過
            db.session.commit()

        monkeypatch.setattr(voucher_mod, "record_upload", fake_record_upload)

        with app.app_context():
            with pytest.raises(QuotaExceededError, match="並行アップロード"):
                create_voucher_from_upload(
                    user_id=user.id,
                    journal_entry_id=entry.id,
                    image_bytes=b"x" * 5000,
                    mime_type="image/png",
                )

        # Voucher は巻き戻されて存在しない
        assert Voucher.query.count() == 0
        # 巻き戻し後に AuditLog も残らない (FK CASCADE 経路の検証)
        from app.models.voucher_audit_log import VoucherAuditLog
        assert VoucherAuditLog.query.count() == 0
        # 容量も巻き戻し済み (fake_record_upload で 10001 にセット後、
        # record_delete で 5000 引かれて 5001 残る = 自分の分が消えた状態)
        usage = db.session.get(StorageUsage, user.id)
        assert usage.used_bytes == 5001  # 10001 - 5000


class TestLv2AuditorBlockedFromAttach:
    """Lv2 監査者は vouchers.attach を呼べない (権限バイパス防止)"""

    def test_lv2_acting_auditor_redirected(
        self, db, client, user, accounts, auditor
    ):
        from app.models.audit import AuditGrant
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2,
        )
        db.session.add(grant)
        db.session.commit()

        entry = _make_entry(db, user, accounts)

        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 2

        import io
        resp = client.post(
            f"/vouchers/attach/{entry.id}",
            data={
                "image": (io.BytesIO(b"x" * 100), "test.png", "image/png"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        # Lv2 は attach 禁止 → ダッシュボードにリダイレクト
        assert resp.status_code in (302, 303)
        # Voucher は作成されていない
        assert Voucher.query.count() == 0


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
    申し送り済 (本 PR ではテストのみで挙動は変えない).

    TODO (Phase 5 続編): create_voucher_from_draft の quota 統合 PR で
    本クラスごと削除する (申し送り docstring も同 PR で除去するため)。
    """

    def test_docstring_mentions_phase5_followup(self):
        from app.services.voucher import create_voucher_from_draft
        assert "Phase 5" in (create_voucher_from_draft.__doc__ or "")
