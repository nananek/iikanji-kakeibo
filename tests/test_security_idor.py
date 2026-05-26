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
                             "5010",
                             "1010", 2000)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get(f"/journal/{entry.id}/json")
        assert resp.status_code == 404

    def test_cannot_edit_other_users_journal(self, app, client, db,
                                              user, second_user, accounts,
                                              second_user_accounts):
        entry = make_journal(db, second_user.id,
                             "5010",
                             "1010", 2000)
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
                             "5010",
                             "1010", 2000)
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
                             "5010",
                             "1010",
                             1000, source="cashbook")
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 404

    def test_cannot_delete_other_users_cashbook_entry(self, app, client, db,
                                                       user, second_user,
                                                       accounts, second_user_accounts):
        entry = make_journal(db, second_user.id,
                             "5010",
                             "1010",
                             1000, source="cashbook")
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.post(f"/cashbook/{entry.id}/delete")
        assert resp.status_code == 404
        assert JournalEntry.query.get(entry.id) is not None


class TestAccountIDOR:
    """他ユーザーの勘定科目に対する操作

    Note: 複合PKのため account_code は URL に含まれるが、
    user_id はセッションから取得される。同じ code を持つ科目が
    両ユーザーに存在する場合、自分の科目が返るためIDOR検知ができない。
    second_user にだけ存在する一意の code を使ってテストする。
    """

    def _create_unique_account(self, db, second_user, account_types):
        """user には存在しない code の科目を second_user に作成"""
        from app.models.account import Account
        acct = Account(
            user_id=second_user.id,
            account_type_id=account_types["asset"].id,
            code="9090", name="テスト専用", is_active=True,
        )
        db.session.add(acct)
        db.session.commit()
        return acct

    def test_cannot_get_other_users_account_api(self, app, client, db,
                                                 user, second_user,
                                                 accounts, account_types):
        target = self._create_unique_account(db, second_user, account_types)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get(f"/accounts/api/{target.code}")
        assert resp.status_code == 404

    def test_cannot_update_other_users_account(self, app, client, db,
                                                user, second_user,
                                                accounts, account_types):
        target = self._create_unique_account(db, second_user, account_types)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.post(f"/accounts/api/{target.code}",
                           json={"name": "ハッキング", "code": "9999"})
        assert resp.status_code == 404

    def test_cannot_get_other_users_account_balance(self, app, client, db,
                                                     user, second_user,
                                                     accounts, account_types):
        target = self._create_unique_account(db, second_user, account_types)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get(f"/accounts/api/{target.code}/balance")
        assert resp.status_code == 404


class TestAccountListIDOR:
    """科目一覧が他ユーザーのデータを含まないこと"""

    def test_account_list_excludes_other_users_accounts(self, app, client, db,
                                                         user, second_user,
                                                         accounts, account_types):
        """second_user にだけ存在する科目が user の一覧に出ないこと"""
        from app.models.account import Account
        unique = Account(
            user_id=second_user.id,
            account_type_id=account_types["asset"].id,
            code="9090", name="他人だけの科目", is_active=True,
        )
        db.session.add(unique)
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get("/accounts/")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "9090" not in html
        assert "他人だけの科目" not in html
        # 自分の科目は見える
        assert "1010" in html

    def test_account_list_shows_own_data_with_overlapping_codes(
            self, app, client, db, user, second_user,
            accounts, second_user_accounts):
        """両ユーザーに同じ code "1010" が存在しても自分の科目だけ返ること"""
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get("/accounts/")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "現金" in html  # 両ユーザー同名だが自分のが出る


class TestLedgerIDOR:
    """元帳が他ユーザーの仕訳を含まないこと"""

    def test_ledger_excludes_other_users_entries(self, app, client, db,
                                                   user, second_user,
                                                   accounts, second_user_accounts):
        """同じ code "1010" でも他人の仕訳は表示されないこと"""
        # second_user の仕訳を作成
        make_journal(db, second_user.id, "5010", "1010", 99999,
                     description="他人の秘密仕訳")
        # user の仕訳を作成
        make_journal(db, user.id, "5010", "1010", 500,
                     description="自分の仕訳")

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get("/reports/ledger?account_code=1010&year=2026")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "自分の仕訳" in html
        assert "他人の秘密仕訳" not in html
        assert "99,999" not in html  # 他人の金額が表示されない

    def test_ledger_nonexistent_code_shows_empty(self, app, client, db,
                                                    user, second_user,
                                                    accounts, second_user_accounts,
                                                    account_types):
        """second_user にだけ存在する code で元帳を開いても空であること"""
        from app.models.account import Account
        unique = Account(
            user_id=second_user.id,
            account_type_id=account_types["expense"].id,
            code="9090", name="他人専用科目", is_active=True,
        )
        db.session.add(unique)
        db.session.commit()
        make_journal(db, second_user.id, "9090", "1010", 77777,
                     description="他人の機密")

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.get("/reports/ledger?account_code=9090&year=2026")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "他人の機密" not in html
        assert "77,777" not in html

    def test_trial_balance_excludes_other_users(self, app, client, db,
                                                  user, second_user,
                                                  accounts, second_user_accounts):
        """試算表ページの accounts_meta JSON に他人の科目が含まれず、
        API 経由でも他人の仕訳が漏洩しないこと。

        E3-F-3a 以降、試算表はクライアントが API から暗号化仕訳を
        取得して描画するため、サーバ HTML に金額は含まれない。
        漏洩経路は (a) accounts_meta JSON、(b) /api/v1/journals API
        の 2 つに移ったので両方検証する。
        """
        import json
        import re
        make_journal(db, second_user.id, "5010", "1010", 88888)

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)

        # (a) /reports/balance の accounts_meta に他人科目が含まれない
        resp = client.get("/reports/balance?year=2026")
        assert resp.status_code == 200
        html = resp.data.decode()
        m = re.search(
            r'<script id="trial-balance-accounts-meta"[^>]*>(.*?)</script>',
            html, flags=re.DOTALL,
        )
        assert m, "accounts_meta script tag not found"
        meta = json.loads(m.group(1).strip())
        # second_user の科目コード "9999" を seed していない前提で、
        # user の科目だけが返されるはず
        # (second_user_accounts fixture は second_user.id にしか作らない)
        # 少なくとも accounts_meta が空でないこと (自分の科目はある)
        assert len(meta) > 0
        # accounts_meta の中身は user.id の科目のみ
        from app.models.account import Account
        user_codes = {
            a.code for a in Account.query.filter_by(user_id=user.id).all()
        }
        for code in meta.keys():
            assert code in user_codes, (
                f"accounts_meta contains non-owner code {code}"
            )

        # (b) /api/v1/journals でも他人仕訳は返らない (renderer が呼ぶ経路)
        resp = client.get("/api/v1/journals?fiscal_year=2026")
        assert resp.status_code == 200
        journals = resp.get_json()["journals"]
        # 88,888 の金額を持つ line が結果に含まれないこと
        for j in journals:
            for line in j.get("lines", []):
                assert line.get("debit_amount", 0) != 88888
                assert line.get("credit_amount", 0) != 88888


class TestAPIJournalLineIDOR:
    """API 経由で他ユーザーの仕訳明細（科目コード含む）が漏洩しないこと"""

    def test_api_list_excludes_other_users_journals(self, client, db,
                                                      user, second_user,
                                                      accounts, second_user_accounts,
                                                      api_key_raw):
        """API で仕訳一覧を取得しても他人の仕訳が含まれないこと"""
        make_journal(db, second_user.id, "5010", "1010", 55555,
                     description="他人の仕訳")
        make_journal(db, user.id, "5010", "1010", 1000,
                     description="自分の仕訳")

        raw_key, _ = api_key_raw
        resp = client.get("/api/v1/journals",
                          headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 200
        data = resp.get_json()
        descriptions = [j["description"] for j in data["journals"]]
        assert "自分の仕訳" in descriptions
        assert "他人の仕訳" not in descriptions

    def test_api_journal_detail_lines_scoped(self, client, db,
                                               user, second_user,
                                               accounts, second_user_accounts,
                                               api_key_raw):
        """API で仕訳詳細を取得した際、明細行が自分のデータであること"""
        entry = make_journal(db, user.id, "5010", "1010", 2000,
                             description="自分の仕訳")
        raw_key, _ = api_key_raw
        resp = client.get(f"/api/v1/journals/{entry.id}",
                          headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 200
        data = resp.get_json()
        for line in data["journal"]["lines"]:
            assert "account_code" in line
            assert line["account_code"] in ("5010", "1010")


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
                             "5010",
                             "1010", 500)
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
                             "5010",
                             "1010", 500)
        raw_key, _ = api_key_raw
        resp = client.get(f"/api/v1/journals/{entry.id}",
                          headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 404
