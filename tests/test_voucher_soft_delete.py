"""Phase 5 #70: Voucher 論理削除 + 電帳法証跡永続化."""

import json
from datetime import date as date_type, datetime, timezone

import pytest

from app.models.journal import JournalEntry
from app.models.storage import StorageUsage
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog


MB = 1024 * 1024


@pytest.fixture
def reset_limiter(app):
    try:
        from app.extensions import limiter
        limiter.reset()
    except Exception:
        pass
    yield


@pytest.fixture
def mock_storage(monkeypatch):
    from app.services import storage as storage_module
    from app.views import vouchers as vouchers_module

    deleted = []

    class FakeBackend:
        def delete(self, key):
            deleted.append(key)

        def get(self, key):
            return b""

    backend = FakeBackend()
    monkeypatch.setattr(storage_module, "get_storage_backend",
                        lambda: backend)
    monkeypatch.setattr(vouchers_module, "get_storage_backend",
                        lambda: backend)
    return {"deleted": deleted, "backend": backend}


_entry_counter = [0]


def _make_voucher(db, user, *, file_size=1 * MB):
    _entry_counter[0] += 1
    entry = JournalEntry(
        user_id=user.id,
        entry_number=_entry_counter[0],
    )
    db.session.add(entry)
    db.session.flush()
    v = Voucher(
        user_id=user.id, journal_entry_id=entry.id,
        image_key=f"vouchers/{user.id}/v.jpg",
        image_mime="image/jpeg",
        file_hash="a" * 64, file_size=file_size,
    )
    db.session.add(v)
    existing_usage = db.session.get(StorageUsage, user.id)
    if existing_usage is None:
        db.session.add(StorageUsage(user_id=user.id, used_bytes=file_size))
    else:
        existing_usage.used_bytes += file_size
    db.session.commit()
    return v


class TestVoucherActiveScope:
    """`Voucher.active()` が deleted_at IS NULL を絞り込む."""

    def test_active_excludes_deleted(self, db, user):
        v1 = _make_voucher(db, user, file_size=100)
        v2 = _make_voucher(db, user, file_size=200)
        v2.deleted_at = datetime.now(timezone.utc)
        db.session.commit()

        active_ids = {v.id for v in Voucher.active().all()}
        assert v1.id in active_ids
        assert v2.id not in active_ids
        # 物理 row は残る
        assert db.session.get(Voucher, v2.id) is not None

    def test_is_deleted_property(self, db, user):
        v = _make_voucher(db, user)
        assert v.is_deleted is False
        v.deleted_at = datetime.now(timezone.utc)
        assert v.is_deleted is True


class TestSoftDeletePersistsAuditLog:
    """論理削除により VoucherAuditLog が永続化される (電帳法証跡)."""

    def test_delete_creates_deleted_audit_log_with_detail(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        v = _make_voucher(db, user, file_size=512)
        image_key, file_hash, vid = v.image_key, v.file_hash, v.id
        resp = logged_in_client.post(
            f"/vouchers/{vid}/delete", follow_redirects=False,
        )
        assert resp.status_code == 302

        logs = VoucherAuditLog.query.filter_by(
            voucher_id=vid, action="deleted",
        ).all()
        assert len(logs) == 1
        # E4 PR-D: 平文 detail は書かない。電帳法の「訂正削除の事実と内容を
        # 確認できる」要件は action="deleted" + 論理削除で残る voucher 行の
        # 各列 (image_key / file_hash(cipher) / file_size) で担保される。
        assert logs[0].encrypted_detail_blob is None
        deleted = db.session.get(Voucher, vid)
        assert deleted is not None and deleted.deleted_at is not None
        assert deleted.image_key == image_key
        assert deleted.file_hash == file_hash
        assert deleted.file_size == 512

    def test_delete_row_remains_in_db(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        """物理 row は残るため `voucher_audit_logs.voucher_id` の FK
        RESTRICT も問題なく動作する."""
        v = _make_voucher(db, user, file_size=100)
        resp = logged_in_client.post(
            f"/vouchers/{v.id}/delete", follow_redirects=False,
        )
        assert resp.status_code == 302

        # 物理 row は残る
        row = db.session.get(Voucher, v.id)
        assert row is not None
        assert row.deleted_at is not None
        # active() からは除外
        assert Voucher.active().filter_by(id=v.id).first() is None

    def test_storage_file_deleted_on_soft_delete(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        """論理削除でも画像ファイル本体はストレージから即削除 (容量解放)."""
        v = _make_voucher(db, user, file_size=100)
        image_key = v.image_key

        logged_in_client.post(
            f"/vouchers/{v.id}/delete", follow_redirects=False,
        )

        # ストレージ delete が呼ばれている
        assert image_key in mock_storage["deleted"]


class TestSoftDeletedHiddenFromQueries:
    """論理削除済 Voucher が各 view / API から非表示."""

    def test_hidden_from_voucher_index(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        # E3-F PR-D-4-4: 一覧はクライアント描画。サーバは証憑メタ JSON
        # (vouchers-index-meta) を渡すだけなので、論理削除済が meta から
        # 除外されることを検証する (画像 URL は client が meta から構築)。
        v = _make_voucher(db, user)
        id_token = f'"id": {v.id}'

        # 1. 削除前は meta に voucher id が含まれる
        resp = logged_in_client.get("/vouchers/")
        assert resp.status_code == 200
        body_before = resp.get_data(as_text=True)
        assert id_token in body_before

        # 2. 削除
        logged_in_client.post(
            f"/vouchers/{v.id}/delete", follow_redirects=False,
        )

        # 3. 削除後は meta が空配列になり voucher id が消える
        resp = logged_in_client.get("/vouchers/")
        assert resp.status_code == 200
        body_after = resp.get_data(as_text=True)
        assert id_token not in body_after
        assert '<script id="vouchers-index-meta" type="application/json">\n[]' in body_after

    def test_hidden_from_verify_endpoint(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        v = _make_voucher(db, user)
        logged_in_client.post(
            f"/vouchers/{v.id}/delete", follow_redirects=False,
        )
        # 論理削除後の verify は 404
        resp = logged_in_client.post(
            f"/vouchers/{v.id}/verify", follow_redirects=False,
        )
        assert resp.status_code == 404


class TestEntryActiveVouchers:
    """`JournalEntry.active_vouchers` リレーションシップが削除済 Voucher
    を除外する (PR #94 review Finding 1: SQL レベルフィルタ化)."""

    def test_active_vouchers_excludes_deleted(self, db, user):
        from app.models.journal import JournalEntry

        v1 = _make_voucher(db, user, file_size=100)
        v2 = _make_voucher(db, user, file_size=200)
        v2.deleted_at = datetime.now(timezone.utc)
        db.session.commit()

        entry1 = db.session.get(JournalEntry, v1.journal_entry_id)
        entry2 = db.session.get(JournalEntry, v2.journal_entry_id)

        # entry1: 1 件 active
        assert len(entry1.active_vouchers) == 1
        assert entry1.active_vouchers[0].id == v1.id

        # entry2: backref には残るが active_vouchers では空
        assert len(entry2.vouchers) == 1
        assert len(entry2.active_vouchers) == 0


class TestApiVoucherLogsAfterSoftDelete:
    """`api_voucher_logs` は論理削除後も AuditLog を引き続き参照可能
    (PR #94 review Finding 3: 電帳法証跡の連環性確認)."""

    def test_logs_accessible_after_soft_delete(
        self, db, user, mock_storage, reset_limiter,
    ):
        """削除済 Voucher の AuditLog も `Voucher.query` 経由で参照可能."""
        from app.extensions import db as app_db
        from app.models.api_key import APIKey
        from flask import url_for

        # API キー発行
        raw, key_hash, key_prefix = APIKey.generate()
        app_db.session.add(APIKey(
            user_id=user.id, name="test", key_hash=key_hash,
            key_prefix=key_prefix,
            scopes="journals:read,journals:create,journals:delete,ai:analyze",
        ))
        app_db.session.commit()

        # 通常の Voucher 作成 + AuditLog 1 件
        v = _make_voucher(db, user)
        app_db.session.add(VoucherAuditLog(
            voucher_id=v.id, user_id=user.id, action="hash_verified",
        ))
        app_db.session.commit()

        # 論理削除
        v.deleted_at = datetime.now(timezone.utc)
        # 電帳法証跡として action="deleted" の AuditLog も追加
        app_db.session.add(VoucherAuditLog(
            voucher_id=v.id, user_id=user.id, action="deleted",
        ))
        app_db.session.commit()

        from flask import current_app
        with current_app.test_client() as c:
            resp = c.get(
                f"/api/v1/vouchers/{v.id}/logs",
                headers={"Authorization": f"Bearer {raw}"},
            )
        # 削除済でも logs エンドポイントは 200 で全 AuditLog を返す
        assert resp.status_code == 200
        data = resp.get_json()
        actions = {log["action"] for log in data["logs"]}
        assert "hash_verified" in actions
        assert "deleted" in actions
