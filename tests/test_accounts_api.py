"""勘定科目管理 API のテスト"""

import json
from datetime import date

import pytest

from app.models.account import Account
from app.models.journal import JournalEntry
from tests.conftest import make_journal


class TestIndex:
    """GET /accounts/ — 科目一覧画面"""

    def test_index_returns_html(self, db, logged_in_client, accounts, account_types):
        resp = logged_in_client.get("/accounts/")
        assert resp.status_code == 200


class TestApiGet:
    """GET /accounts/api/<code> — 編集・コピー用データ取得"""

    def test_get_account(self, db, logged_in_client, accounts):
        resp = logged_in_client.get("/accounts/api/5010")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["code"] == "5010"
        assert data["name"] == "食費"
        # normal_balance はクライアントの残高復号集計 (符号判定) 用に返す (#338 B)。
        assert data["normal_balance"] == "debit"

    def test_get_account_copy(self, db, logged_in_client, accounts):
        """コピーモードではコードがインクリメントされ、system フラグがクリアされる"""
        resp = logged_in_client.get("/accounts/api/5010?copy=1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["code"] != "5010"
        assert data["is_system"] is False
        assert data["system_role"] == ""

    def test_copy_system_role_rejected(self, db, logged_in_client, accounts):
        """system_role を持つ科目はコピー不可"""
        resp = logged_in_client.get("/accounts/api/3010?copy=1")
        assert resp.status_code == 400

    def test_get_nonexistent(self, db, logged_in_client, accounts):
        resp = logged_in_client.get("/accounts/api/9999")
        assert resp.status_code == 404


class TestApiCreate:
    """POST /accounts/api/new — 新規科目作成"""

    def test_create_account(self, db, logged_in_client, accounts, account_types):
        resp = logged_in_client.post(
            "/accounts/api/new",
            data=json.dumps({
                "code": "5099",
                "name": "テスト科目",
                "account_type_id": account_types["expense"].id,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert Account.query.filter_by(code="5099").first() is not None

    def test_create_duplicate_code(self, db, logged_in_client, accounts, account_types):
        resp = logged_in_client.post(
            "/accounts/api/new",
            data=json.dumps({
                "code": "5010",
                "name": "重複",
                "account_type_id": account_types["expense"].id,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "既に使われています" in resp.get_json()["error"]

    def test_create_missing_code(self, db, logged_in_client, accounts, account_types):
        resp = logged_in_client.post(
            "/accounts/api/new",
            data=json.dumps({
                "code": "",
                "name": "名前あり",
                "account_type_id": account_types["expense"].id,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_missing_name(self, db, logged_in_client, accounts, account_types):
        resp = logged_in_client.post(
            "/accounts/api/new",
            data=json.dumps({
                "code": "5099",
                "name": "",
                "account_type_id": account_types["expense"].id,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_code_too_long(self, db, logged_in_client, accounts, account_types):
        resp = logged_in_client.post(
            "/accounts/api/new",
            data=json.dumps({
                "code": "A" * 11, "name": "長いコード",
                "account_type_id": account_types["expense"].id,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "10文字以内" in resp.get_json()["error"]

    def test_create_name_too_long(self, db, logged_in_client, accounts, account_types):
        resp = logged_in_client.post(
            "/accounts/api/new",
            data=json.dumps({
                "code": "5099", "name": "あ" * 101,
                "account_type_id": account_types["expense"].id,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "100文字以内" in resp.get_json()["error"]

    def test_create_missing_account_type(self, db, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/accounts/api/new",
            data=json.dumps({"code": "5099", "name": "テスト"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "科目区分は必須" in resp.get_json()["error"]


class TestApiUpdate:
    """POST /accounts/api/<code> — 科目更新"""

    def test_update_name(self, db, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({"code": "5010", "name": "食費（変更後）", "is_active": True}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        acct = Account.query.filter_by(code="5010").first()
        assert acct.name == "食費（変更後）"

    def test_update_system_account_code_readonly(self, db, logged_in_client, accounts):
        """is_system な科目はコード変更が無視される"""
        acct = Account.query.filter_by(code="3010").first()
        acct.is_system = True
        db.session.commit()

        resp = logged_in_client.post(
            "/accounts/api/3010",
            data=json.dumps({"code": "3099", "name": "元入金改", "is_active": True}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert Account.query.filter_by(code="3010").first() is not None

    def test_cannot_deactivate_system_role(self, db, logged_in_client, accounts):
        """system_role を持つ科目は無効化できない"""
        resp = logged_in_client.post(
            "/accounts/api/3010",
            data=json.dumps({"name": "元入金", "is_active": False}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "無効化できません" in resp.get_json()["error"]

    def test_update_empty_name(self, db, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({"code": "5010", "name": "", "is_active": True}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "科目名は必須" in resp.get_json()["error"]

    def test_update_name_too_long(self, db, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({"code": "5010", "name": "あ" * 101, "is_active": True}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "100文字以内" in resp.get_json()["error"]

    def test_deactivate_with_balance_succeeds_server_side(self, db, logged_in_client, user, accounts):
        """E2EE 化 (#338 B): サーバは残高を計算できないため、残高有無に関わらず
        無効化を許可する。残高がある科目を弾くのはクライアント側 (accountEditor が
        先に暗号化振替で残高を 0 にしてから本 POST する) の責務。サーバは平文の
        振替仕訳を作らない。
        """
        make_journal(db, user.id, "5010", "1010", 3000)
        before = JournalEntry.query.filter_by(user_id=user.id).count()
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({"code": "5010", "name": "食費", "is_active": False}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        acct = Account.query.filter_by(code="5010").first()
        assert acct.is_active is False
        assert acct.deactivated_year == date.today().year
        # サーバは振替仕訳を作らない (件数 before==after で判定)。
        assert JournalEntry.query.filter_by(user_id=user.id).count() == before

    def test_deactivate_ignores_transfer_code(
        self, db, logged_in_client, user, accounts
    ):
        """残高振替はクライアントの暗号化経路へ移行したため、サーバは
        transfer_to_account_code を無視し、平文の振替仕訳を作らない。
        """
        make_journal(db, user.id, "5010", "1010", 3000)
        before = JournalEntry.query.filter_by(user_id=user.id).count()
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({
                "code": "5010", "name": "食費", "is_active": False,
                "transfer_to_account_code": "5020",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        # 振替仕訳は作られない (件数 before==after で判定)。
        after = JournalEntry.query.filter_by(user_id=user.id).count()
        assert after == before
        assert Account.query.filter_by(code="5010").first().is_active is False

    def test_deactivate_zero_balance_ok(self, db, logged_in_client, user, accounts):
        """残高 0 (クライアント振替後の状態) でも無効化でき、サーバは仕訳を追加しない。"""
        # 借方 3000 / 貸方 3000 を計上して 5010 の残高を 0 にする
        make_journal(db, user.id, "5010", "1010", 3000)
        make_journal(db, user.id, "1010", "5010", 3000)
        before = JournalEntry.query.filter_by(user_id=user.id).count()
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({"code": "5010", "name": "食費", "is_active": False}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        acct = Account.query.filter_by(code="5010").first()
        assert acct.is_active is False
        assert acct.deactivated_year == date.today().year
        assert JournalEntry.query.filter_by(user_id=user.id).count() == before

    def test_reactivate_clears_deactivated_year(self, db, logged_in_client, user, accounts):
        acct = Account.query.filter_by(code="5010").first()
        acct.is_active = False
        acct.deactivated_year = 2025
        db.session.commit()
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({"code": "5010", "name": "食費", "is_active": True}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert Account.query.filter_by(code="5010").first().deactivated_year is None

    def test_update_empty_code(self, db, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({"code": "", "name": "食費", "is_active": True}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_update_code_too_long(self, db, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({"code": "A" * 11, "name": "食費", "is_active": True}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_update_duplicate_code(self, db, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({"code": "5020", "name": "食費", "is_active": True}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "既に使われています" in resp.get_json()["error"]

    def test_update_account_type_change(self, db, logged_in_client, accounts, account_types):
        resp = logged_in_client.post(
            "/accounts/api/5010",
            data=json.dumps({
                "code": "5010", "name": "食費", "is_active": True,
                "account_type_id": account_types["asset"].id,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert Account.query.filter_by(code="5010").first().account_type_id == account_types["asset"].id


class TestNextCode:
    """_next_code のロジック確認（API経由）"""

    def test_copy_increments_code(self, db, logged_in_client, accounts):
        """5010 → 5011（5020 は既存なのでスキップされない: 5011が空き）"""
        resp = logged_in_client.get("/accounts/api/5010?copy=1")
        data = resp.get_json()
        assert data["code"] == "5011"

    def test_copy_skips_existing(self, db, logged_in_client, user, accounts, account_types):
        """5010 の次の 5011 が既存なら 5012 になる"""
        db.session.add(Account(
            user_id=user.id, account_type_id=account_types["expense"].id,
            code="5011", name="テスト", is_active=True, display_order=0,
        ))
        db.session.commit()

        resp = logged_in_client.get("/accounts/api/5010?copy=1")
        data = resp.get_json()
        assert data["code"] == "5012"
