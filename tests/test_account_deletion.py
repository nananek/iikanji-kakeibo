"""退会フロー (Phase 4 公開運用整備)."""

from datetime import date as date_type, datetime, timezone
from unittest.mock import patch

import pytest

from app.models.account import Account
from app.models.api_key import APIKey
from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.storage import StorageUsage
from app.models.user import User
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.models.webauthn import WebAuthnCredential


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
    from app.services import account_deletion as ad_module

    deleted = []

    class FakeBackend:
        def delete(self, key):
            deleted.append(key)

        def get(self, key):
            return b""

    backend = FakeBackend()
    monkeypatch.setattr(storage_module, "get_storage_backend",
                        lambda: backend)
    monkeypatch.setattr(ad_module, "get_storage_backend",
                        lambda: backend)
    return {"deleted": deleted, "backend": backend}


def _make_user_with_data(db, *, username, email):
    """退会テスト用にデータを一通り投入したユーザーを作る."""
    user = User(
        username=username, email=email, user_type="personal",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()

    # Journal Entry
    entry = JournalEntry(
        user_id=user.id, date=date_type(2026, 5, 1),
        entry_number=1, description="テスト仕訳",
    )
    db.session.add(entry)
    db.session.flush()

    # Voucher + AuditLog
    voucher = Voucher(
        user_id=user.id, journal_entry_id=entry.id,
        image_key=f"vouchers/{user.id}/v.jpg",
        image_mime="image/jpeg",
        file_hash="a" * 64, file_size=100,
    )
    db.session.add(voucher)
    db.session.flush()
    log = VoucherAuditLog(
        voucher_id=voucher.id, user_id=user.id,
        action="attached", detail="{}",
    )
    db.session.add(log)

    # APIKey
    raw, key_hash, key_prefix = APIKey.generate()
    db.session.add(APIKey(
        user_id=user.id, name="key", key_hash=key_hash,
        key_prefix=key_prefix, scopes="journals:read",
    ))

    # StorageUsage
    db.session.add(StorageUsage(user_id=user.id, used_bytes=100))

    db.session.commit()
    return user, voucher, log, entry


class TestDeleteUserAccountService:
    """`delete_user_account` 関数の挙動."""

    def test_user_row_physically_deleted(self, db, mock_storage):
        from app.services.account_deletion import delete_user_account

        user, _v, _log, _entry = _make_user_with_data(
            db, username="alice", email="alice@example.com",
        )
        user_id = user.id

        delete_user_account(user_id)

        # User row が物理削除されている
        assert db.session.get(User, user_id) is None

    def test_related_data_deleted(self, db, mock_storage):
        from app.services.account_deletion import delete_user_account

        user, voucher, _log, entry = _make_user_with_data(
            db, username="bob", email="bob@example.com",
        )
        user_id = user.id
        voucher_id = voucher.id
        entry_id = entry.id

        delete_user_account(user_id)

        # 関連データ全て削除されている
        assert db.session.get(Voucher, voucher_id) is None
        assert db.session.get(JournalEntry, entry_id) is None
        assert APIKey.query.filter_by(user_id=user_id).count() == 0
        assert db.session.get(StorageUsage, user_id) is None

    def test_voucher_audit_log_preserved_anonymized(self, db, mock_storage):
        """電帳法保管: VoucherAuditLog は user_id / voucher_id を NULL 化して残る."""
        from app.services.account_deletion import delete_user_account

        user, voucher, log, _entry = _make_user_with_data(
            db, username="carol", email="carol@example.com",
        )
        user_id = user.id
        voucher_id = voucher.id
        log_id = log.id

        delete_user_account(user_id)

        # AuditLog 自体は残る
        preserved = db.session.get(VoucherAuditLog, log_id)
        assert preserved is not None
        # user_id は NULL 化 (delete_user_account 内で明示)
        assert preserved.user_id is None
        # voucher_id は ondelete=SET NULL で自動 NULL 化
        # SQLite では ondelete が動かないので、user_id NULL 化のみ保証
        # PostgreSQL では voucher_id も NULL 化される
        # 内容 (action / detail) は保持
        assert preserved.action == "attached"

    def test_storage_files_deleted(self, db, mock_storage):
        from app.services.account_deletion import delete_user_account

        user, voucher, _log, _entry = _make_user_with_data(
            db, username="dave", email="dave@example.com",
        )
        image_key = voucher.image_key

        delete_user_account(user.id)

        # ストレージから画像削除が呼ばれている
        assert image_key in mock_storage["deleted"]

    def test_other_user_data_not_affected(self, db, mock_storage):
        """別ユーザーのデータは影響を受けない."""
        from app.services.account_deletion import delete_user_account

        user_a, voucher_a, _log_a, _entry_a = _make_user_with_data(
            db, username="alice2", email="alice2@example.com",
        )
        user_b, voucher_b, log_b, _entry_b = _make_user_with_data(
            db, username="bob2", email="bob2@example.com",
        )
        user_b_id = user_b.id
        voucher_b_id = voucher_b.id

        # A だけ削除
        delete_user_account(user_a.id)

        # B のデータは無傷
        assert db.session.get(User, user_b_id) is not None
        assert db.session.get(Voucher, voucher_b_id) is not None
        b_log_count = VoucherAuditLog.query.filter_by(
            user_id=user_b_id,
        ).count()
        assert b_log_count > 0


class TestDeleteAccountView:
    """`/settings/delete-account` エンドポイント."""

    def test_get_renders_form(self, logged_in_client, reset_limiter):
        resp = logged_in_client.get("/settings/delete-account")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "アカウント削除" in body
        assert "name=\"password\"" in body
        assert "name=\"confirm\"" in body

    def test_post_wrong_password_keeps_user(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        resp = logged_in_client.post("/settings/delete-account", data={
            "password": "wrong-password",
            "confirm": "y",
        })
        # フォーム再描画 (200) + User row は残る
        # flash メッセージは showToast の JSON ペイロード内に Unicode
        # escape された形で埋め込まれるため、表示内容ではなく User row
        # の残存と status code でガードする
        assert resp.status_code == 200
        assert db.session.get(User, user.id) is not None

    def test_post_requires_confirm_checkbox(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        resp = logged_in_client.post("/settings/delete-account", data={
            "password": "password123",
            # confirm を渡さない
        })
        # フォームバリデーションエラーで再描画 + User 残存
        assert resp.status_code == 200
        assert db.session.get(User, user.id) is not None

    def test_post_blocked_during_proxy_view(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        """代理閲覧中は削除禁止."""
        with logged_in_client.session_transaction() as sess:
            sess["acting_as_user_id"] = 999

        resp = logged_in_client.post("/settings/delete-account", data={
            "password": "password123", "confirm": "y",
        })
        # リダイレクトされて User 残存
        assert resp.status_code == 302
        assert db.session.get(User, user.id) is not None

    def test_post_success_deletes_and_sends_email(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        sent = []
        with patch(
            "app.views.settings.send_email",
            side_effect=lambda to, t, ctx=None, **kw: sent.append(
                (to, t, ctx or {})
            ),
        ):
            resp = logged_in_client.post(
                "/settings/delete-account",
                data={"password": "password123", "confirm": "y"},
                follow_redirects=False,
            )

        # ログイン画面へリダイレクト
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        # User row 物理削除
        assert db.session.get(User, user.id) is None
        # account_deleted メール送信
        assert any(s[1] == "account_deleted" for s in sent)
        admin = next(s for s in sent if s[1] == "account_deleted")
        assert admin[0] == "test@example.com"
        assert admin[2]["username"] == "testuser"

    def test_passkey_only_user_can_delete_without_password(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        """Passkey 専用ユーザーはパスワード入力なしで退会できる (GDPR 消去権)."""
        user.passkey_only_login = True
        db.session.commit()

        sent = []
        with patch(
            "app.views.settings.send_email",
            side_effect=lambda to, t, ctx=None, **kw: sent.append((to, t)),
        ):
            resp = logged_in_client.post(
                "/settings/delete-account",
                data={"confirm": "y"},  # password 未送信
                follow_redirects=False,
            )

        # ログイン画面へリダイレクト + User row 削除
        assert resp.status_code == 302
        assert db.session.get(User, user.id) is None
        assert any(s[1] == "account_deleted" for s in sent)

    def test_passkey_user_still_needs_confirm_checkbox(
        self, logged_in_client, db, user, mock_storage, reset_limiter,
    ):
        """Passkey 専用ユーザーでも confirm チェックは必須."""
        user.passkey_only_login = True
        db.session.commit()

        resp = logged_in_client.post(
            "/settings/delete-account",
            data={},  # confirm も未送信
            follow_redirects=False,
        )
        # フォーム再描画 + User 残存
        assert resp.status_code == 200
        assert db.session.get(User, user.id) is not None

    def test_passkey_only_template_hides_password_field(
        self, logged_in_client, db, user, reset_limiter,
    ):
        """Passkey 専用ユーザー向けにパスワードフィールドが非表示."""
        user.passkey_only_login = True
        db.session.commit()

        resp = logged_in_client.get("/settings/delete-account")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # パスワード入力欄が出ない (input type=password がない)
        assert "type=\"password\"" not in body
        # 代わりに案内が出る
        assert "Passkey 専用アカウント" in body


class TestJournalEntryDeletionOrder:
    """JournalEntryLine → JournalEntry の順序削除 (PostgreSQL FK 制約対応)."""

    def test_journal_entry_line_deleted_before_entry(
        self, db, mock_storage, account_types
    ):
        from app.services.account_deletion import delete_user_account

        user, _v, _log, entry = _make_user_with_data(
            db, username="orderer", email="o@example.com",
        )
        # account + JournalEntryLine を追加 (entry に紐づく)
        from app.models.account import Account
        from app.models.journal import JournalEntryLine

        db.session.add(Account(
            user_id=user.id,
            account_type_id=account_types["asset"].id,
            code="9999", name="テスト科目", is_active=True,
        ))
        db.session.flush()
        db.session.add(JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user.id,
            account_code="9999",
            debit_amount=100, credit_amount=0,
            description="テスト",
        ))
        db.session.commit()
        assert JournalEntryLine.query.filter_by(
            journal_entry_id=entry.id,
        ).count() == 1

        delete_user_account(user.id)

        # JournalEntryLine と JournalEntry の両方が削除されている
        # (順序削除なしだと PostgreSQL の FK 制約で失敗する)
        assert JournalEntryLine.query.filter_by(
            journal_entry_id=entry.id,
        ).count() == 0
        assert db.session.get(JournalEntry, entry.id) is None
