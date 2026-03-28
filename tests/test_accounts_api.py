"""勘定科目管理 API のテスト"""

import json
import pytest

from app.models.account import Account


class TestApiGet:
    """GET /accounts/api/<code> — 編集・コピー用データ取得"""

    def test_get_account(self, db, logged_in_client, accounts):
        resp = logged_in_client.get("/accounts/api/5010")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["code"] == "5010"
        assert data["name"] == "食費"

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
