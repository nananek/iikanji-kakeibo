"""IDOR（Insecure Direct Object Reference）テスト — 他ユーザーリソースへの不正アクセス防止"""

import pytest

from app.models.journal import JournalEntry
from app.models.api_key import APIKey
from tests.conftest import make_journal


class TestJournalIDOR:
    """他ユーザーの仕訳に対する操作"""

    def test_cannot_view_other_users_journal_json(self, app, client, db,
                                                   user, second_user, accounts,
                                                   second_user_accounts):
        entry = make_journal(db, second_user.id,
                             second_user_accounts["5010"].id,
                             second_user_accounts["1010"].id, 2000)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get(f"/journal/{entry.id}/json")
        assert resp.status_code == 404

    def test_cannot_edit_other_users_journal(self, app, client, db,
                                              user, second_user, accounts,
                                              second_user_accounts):
        entry = make_journal(db, second_user.id,
                             second_user_accounts["5010"].id,
                             second_user_accounts["1010"].id, 2000)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.post(f"/journal/{entry.id}/edit-api",
                           json={"date": "2026-01-15", "description": "hacked",
                                 "lines": []})
        assert resp.status_code == 404

    def test_cannot_delete_other_users_journal(self, app, client, db,
                                                user, second_user, accounts,
                                                second_user_accounts):
        entry = make_journal(db, second_user.id,
                             second_user_accounts["5010"].id,
                             second_user_accounts["1010"].id, 2000)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.post(f"/journal/{entry.id}/delete")
        assert resp.status_code == 404
        assert JournalEntry.query.get(entry.id) is not None


class TestCashbookIDOR:
    """他ユーザーの出納帳仕訳に対する操作"""

    def test_cannot_edit_other_users_cashbook_entry(self, app, client, db,
                                                     user, second_user,
                                                     accounts, second_user_accounts):
        entry = make_journal(db, second_user.id,
                             second_user_accounts["5010"].id,
                             second_user_accounts["1010"].id,
                             1000, source="cashbook")
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 404

    def test_cannot_delete_other_users_cashbook_entry(self, app, client, db,
                                                       user, second_user,
                                                       accounts, second_user_accounts):
        entry = make_journal(db, second_user.id,
                             second_user_accounts["5010"].id,
                             second_user_accounts["1010"].id,
                             1000, source="cashbook")
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.post(f"/cashbook/{entry.id}/delete")
        assert resp.status_code == 404
        assert JournalEntry.query.get(entry.id) is not None


class TestAccountIDOR:
    """他ユーザーの勘定科目に対する操作"""

    def test_cannot_get_other_users_account_api(self, app, client, db,
                                                 user, second_user,
                                                 accounts, second_user_accounts):
        target = second_user_accounts["1010"]
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get(f"/accounts/api/{target.id}")
        assert resp.status_code == 404

    def test_cannot_update_other_users_account(self, app, client, db,
                                                user, second_user,
                                                accounts, second_user_accounts):
        target = second_user_accounts["1010"]
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.post(f"/accounts/api/{target.id}",
                           json={"name": "ハッキング", "code": "9999"})
        assert resp.status_code == 404

    def test_cannot_get_other_users_account_balance(self, app, client, db,
                                                     user, second_user,
                                                     accounts, second_user_accounts):
        target = second_user_accounts["1010"]
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get(f"/accounts/api/{target.id}/balance")
        assert resp.status_code == 404


class TestSettingsIDOR:
    """他ユーザーの設定リソースに対する操作"""

    def test_cannot_delete_other_users_api_key(self, app, client, db,
                                                user, second_user):
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(user_id=second_user.id, name="victim-key",
                     key_hash=key_hash, key_prefix=key_prefix,
                     scopes="journals:read", is_active=True)
        db.session.add(key)
        db.session.commit()
        key_id = key.id

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.post(f"/settings/api-keys/{key_id}/delete")
        assert resp.status_code == 404
        assert APIKey.query.get(key_id) is not None

    def test_cannot_delete_other_users_passkey(self, app, client, db,
                                                user, second_user):
        from app.models.webauthn import WebAuthnCredential
        cred = WebAuthnCredential(
            user_id=second_user.id,
            credential_id=b"fakeid",
            credential_public_key=b"fakekey",
            current_sign_count=0,
            name="victim-passkey",
        )
        db.session.add(cred)
        db.session.commit()
        cred_id = cred.id

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.post(f"/settings/passkeys/{cred_id}/delete")
        assert resp.status_code == 404


class TestAPIUserIsolation:
    """API Bearer 認証でのユーザー分離（既存テストの補完）"""

    def test_cannot_delete_other_users_journal_via_api(self, client, db,
                                                        user, second_user,
                                                        accounts, second_user_accounts,
                                                        api_key_raw):
        """user の API キーで second_user の仕訳を削除できない"""
        entry = make_journal(db, second_user.id,
                             second_user_accounts["5010"].id,
                             second_user_accounts["1010"].id, 500)
        raw_key, _ = api_key_raw
        resp = client.delete(f"/api/v1/journals/{entry.id}",
                             headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 404
        assert JournalEntry.query.get(entry.id) is not None

    def test_cannot_view_other_users_journal_detail_via_api(self, client, db,
                                                             user, second_user,
                                                             accounts, second_user_accounts,
                                                             api_key_raw):
        entry = make_journal(db, second_user.id,
                             second_user_accounts["5010"].id,
                             second_user_accounts["1010"].id, 500)
        raw_key, _ = api_key_raw
        resp = client.get(f"/api/v1/journals/{entry.id}",
                          headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 404
