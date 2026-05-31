"""モデルとseedのテスト — APIKey・JournalEntry・seed"""

from datetime import date

import pytest

from app.models.api_key import APIKey, KEY_PREFIX
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.user import User
from app.services.seed import seed_account_types, seed_accounts_for_user


class TestUser:
    def test_set_and_check_password(self, db):
        u = User(username="u1", email="u1@test.com", user_type="personal")
        u.set_password("mypassword")
        db.session.add(u)
        db.session.commit()
        assert u.check_password("mypassword") is True
        assert u.check_password("wrongpassword") is False

    def test_default_user_type(self, db, user):
        assert user.user_type == "personal"


class TestAPIKey:
    def test_generate(self):
        raw_key, key_hash, key_prefix = APIKey.generate()
        assert raw_key.startswith(KEY_PREFIX)
        assert len(raw_key) == len(KEY_PREFIX) + 64  # ik_ + 64 hex chars
        assert key_hash == APIKey.hash_key(raw_key)
        assert key_prefix.endswith("...")

    def test_has_scope(self, db, user):
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="test",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:create,journals:read", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        assert key.has_scope("journals:create") is True
        assert key.has_scope("journals:read") is True
        assert key.has_scope("journals:delete") is False
        assert key.has_scope("ai:analyze") is False

    def test_scope_list(self, db, user):
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="test",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:create,ai:analyze", is_active=True,
        )
        assert key.scope_list == ["journals:create", "ai:analyze"]

    def test_hash_consistency(self):
        raw_key, _, _ = APIKey.generate()
        h1 = APIKey.hash_key(raw_key)
        h2 = APIKey.hash_key(raw_key)
        assert h1 == h2

    def test_different_keys_different_hashes(self):
        raw1, _, _ = APIKey.generate()
        raw2, _, _ = APIKey.generate()
        assert APIKey.hash_key(raw1) != APIKey.hash_key(raw2)


class TestJournalEntry:
    def test_is_balanced(self, db, user, accounts):
        entry = JournalEntry(
            user_id=user.id,
            entry_number=1,
        )
        entry.lines = [
            JournalEntryLine(account_user_id=user.id,
                             account_code="5010",
                             debit_amount=1000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id,
                             account_code="1010",
                             debit_amount=0, credit_amount=1000),
        ]
        db.session.add(entry)
        db.session.commit()
        assert entry.is_balanced is True
        assert entry.total_debit == 1000
        assert entry.total_credit == 1000

    def test_cascade_delete(self, db, user, accounts):
        from tests.conftest import make_journal
        entry = make_journal(db, user.id, "5010", "1010", 500)
        entry_id = entry.id
        line_count = len(entry.lines)
        assert line_count == 2
        db.session.delete(entry)
        db.session.commit()
        assert JournalEntryLine.query.filter_by(journal_entry_id=entry_id).count() == 0


class TestSeed:
    def test_seed_account_types(self, db):
        seed_account_types()
        from app.models.account import AccountType
        types = AccountType.query.all()
        codes = {t.code for t in types}
        assert codes >= {"asset", "liability", "equity", "revenue", "expense"}

    def test_seed_accounts_for_user(self, db, user, account_types):
        seed_accounts_for_user(user.id)
        from app.models.account import Account
        accts = Account.query.filter_by(user_id=user.id).all()
        assert len(accts) >= 30  # 最低でも30科目以上

        # system_role が正しく設定されているか
        capital = Account.query.filter_by(user_id=user.id, code="3010").first()
        assert capital is not None
        assert capital.system_role == "capital"

        retained = Account.query.filter_by(user_id=user.id, code="3020").first()
        assert retained.system_role == "retained_earnings"

    def test_seed_idempotent(self, db, user, account_types):
        seed_accounts_for_user(user.id)
        from app.models.account import Account
        count1 = Account.query.filter_by(user_id=user.id).count()
        seed_accounts_for_user(user.id)
        count2 = Account.query.filter_by(user_id=user.id).count()
        assert count1 == count2
