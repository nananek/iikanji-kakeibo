"""出納帳 (cashbook) ビューのテスト

cashbook.py のカバレッジ向上を目的とする。
"""

from datetime import date

from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry
from tests.conftest import make_journal


class TestIndex:
    """E3-F PR-D-4-2: 出納帳一覧はクライアント描画 shell に移行。

    サーバは平文 date/description/金額/科目名を読まず、year + accounts_meta を
    JSON script で渡すだけ。実際の一覧描画は index_renderer.mjs が MK 復号して
    行う (検証は test_cashbook_index_rows.mjs)。
    """

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/cashbook/")
        assert resp.status_code in (302, 401)

    def test_renders_shell(self, logged_in_client, accounts):
        resp = logged_in_client.get("/cashbook/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "cashbook-index-params" in body
        assert "cashbook-index-accounts-meta" in body
        assert "index_renderer.mjs" in body

    def test_year_param_reflected_in_selector(self, logged_in_client, accounts):
        resp = logged_in_client.get("/cashbook/?year=2025")
        assert resp.status_code == 200
        assert 'value="2025" selected' in resp.get_data(as_text=True)

    def test_accounts_meta_includes_account_names(self, logged_in_client, accounts):
        resp = logged_in_client.get("/cashbook/")
        body = resp.get_data(as_text=True)
        # accounts_meta JSON に科目名が含まれる (クライアントが科目名解決に使う)
        assert "食費" in body or "5010" in body

    def test_does_not_render_plaintext_entries(self, db, logged_in_client, user, accounts):
        # サーバは平文 date/description/金額を読まない → HTML に出ない
        make_journal(db, user.id, "5010", "1010", 13579,
                     entry_date=date(2026, 2, 15), source="cashbook")
        resp = logged_in_client.get("/cashbook/?year=2026")
        body = resp.get_data(as_text=True)
        assert "13,579" not in body
        assert "13579" not in body


class TestNewGet:
    def test_unauthenticated(self, client):
        resp = client.get("/cashbook/new")
        assert resp.status_code in (302, 401)

    def test_get_renders_form(self, logged_in_client, accounts):
        resp = logged_in_client.get("/cashbook/new")
        assert resp.status_code == 200


class TestNewPostRejected:
    """E3-F PR-B1.1: cashbook.new は GET 専用。POST は 405 を返し、
    平文 POST 経路でデータが書込まれないことを保証する。"""

    def test_post_returns_405(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "1500",
            "description": "平文 POST",
            "fiscal_period": "",
        })
        assert resp.status_code == 405
        # 仕訳は作成されていない (server-side 経路が無効化されている)。
        # E3-F PR-D-6-5: source 列は DROP 済のため user_id で件数判定する。
        assert JournalEntry.query.filter_by(user_id=user.id).count() == 0


class TestEdit:
    def _make_cashbook(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1500,
                            entry_date=date(2026, 2, 15), source="cashbook",
                            description="編集対象")

    def test_unauthenticated(self, client):
        resp = client.get("/cashbook/1/edit")
        assert resp.status_code in (302, 401)

    def test_get_renders_form_without_plaintext_desc(self, db, logged_in_client, user, accounts):
        """E3-F PR-D-6-3b-3: 平文 description はサーバ描画に焼き込まれず、
        クライアント (edit_form_prefill.js) が encrypted_blob を MK 復号して
        date / description を埋める。フォーム自体は描画される。"""
        entry = self._make_cashbook(db, user.id)
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # 平文 description は HTML に出力されない
        assert "編集対象" not in body
        # クライアント hydration スクリプトが読み込まれる
        assert "edit_form_prefill.js" in body

    def test_404_for_nonexistent(self, logged_in_client, accounts):
        resp = logged_in_client.get("/cashbook/9999/edit")
        assert resp.status_code == 404

    def test_idor_other_user(self, db, logged_in_client, user, accounts,
                             second_user, second_user_accounts):
        """他人の仕訳は 404"""
        other_entry = make_journal(
            db, second_user.id, "5010", "1010", 1000,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        resp = logged_in_client.get(f"/cashbook/{other_entry.id}/edit")
        assert resp.status_code == 404

    def test_post_returns_405(self, db, logged_in_client, user, accounts):
        """E3-F PR-B1.1: cashbook.edit は GET 専用。POST は 405 を返し、
        平文 POST 経路で既存仕訳が書き換えられないことを保証する。"""
        entry = self._make_cashbook(db, user.id)
        resp = logged_in_client.post(f"/cashbook/{entry.id}/edit", data={
            "date": "2026-02-20",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "2500",
            "description": "更新後",
            "fiscal_period": "",
        })
        assert resp.status_code == 405
        # E3-F PR-D-6-5: description 列は DROP 済。POST が 405 で弾かれ仕訳が
        # 残存していることのみ確認する (本文不変は E2EE PUT 経路のテストで担保)。
        db.session.refresh(entry)
        assert entry.id is not None

    def test_edit_blocked_by_closed_period(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        # 確定済みなのでリダイレクト
        assert resp.status_code in (302, 303)

    def test_edit_get_no_plaintext_amount_in_body(self, db, logged_in_client, user, accounts):
        """#338 PR2 (方針B): サーバは取引種類・金額の 3 方向検出 (平文 debit/credit/
        account_code 読み) を撤去した。平文金額が HTML に焼き込まれず、クライアント
        prefill スクリプト (cashbook_prefill.js) と accounts_meta が供給される。"""
        entry = make_journal(
            db, user.id, "5010", "1010", 7321,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # 平文金額 (借方額) が value 属性等に焼き込まれない
        assert 'value="7321"' not in body
        assert "7321" not in body
        # クライアント prefill スクリプトと code→type_code メタが供給される
        assert "cashbook_prefill.js" in body
        assert "_acctMeta" in body

    def test_edit_accounts_meta_includes_type_code(self, db, logged_in_client, user, accounts):
        """#338 PR2: クライアント側 3 方向検出 (BS=asset/liability 判定) のため
        accounts_meta が code→{name, type_code} を含む。"""
        entry = make_journal(
            db, user.id, "1010", "4010", 3000,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 200
        import json as _json
        import re
        body = resp.get_data(as_text=True)
        m = re.search(r"var _acctMeta = (\{.*?\});", body)
        assert m, "_acctMeta JSON が埋め込まれていない"
        meta = _json.loads(m.group(1))
        # 1010 (現金) は asset、4010 (売上) は revenue
        assert meta["1010"]["type_code"] == "asset"
        assert meta["4010"]["type_code"] == "revenue"
        assert "name" in meta["1010"]


class TestDelete:
    def _make_cashbook(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1500,
                            entry_date=date(2026, 2, 15), source="cashbook")

    def test_unauthenticated(self, client):
        resp = client.post("/cashbook/1/delete")
        assert resp.status_code in (302, 401)

    def test_delete_success(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        entry_id = entry.id
        resp = logged_in_client.post(f"/cashbook/{entry_id}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(JournalEntry, entry_id) is None

    def test_delete_with_hx_request(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        entry_id = entry.id
        resp = logged_in_client.post(
            f"/cashbook/{entry_id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
        assert db.session.get(JournalEntry, entry_id) is None

    def test_delete_404_nonexistent(self, logged_in_client, accounts):
        resp = logged_in_client.post("/cashbook/9999/delete")
        assert resp.status_code == 404

    def test_idor_other_user_cannot_delete(self, db, logged_in_client, user, accounts,
                                            second_user, second_user_accounts):
        other_entry = make_journal(
            db, second_user.id, "5010", "1010", 1000,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        resp = logged_in_client.post(f"/cashbook/{other_entry.id}/delete")
        assert resp.status_code == 404
        # 残っている
        assert db.session.get(JournalEntry, other_entry.id) is not None

    def test_delete_blocked_by_closed_period(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(f"/cashbook/{entry.id}/delete")
        # 確定済みでリダイレクト or HX 422
        assert resp.status_code in (302, 303, 422)
        # 削除されていない
        assert db.session.get(JournalEntry, entry.id) is not None

    def test_delete_blocked_by_closed_period_hx(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(
            f"/cashbook/{entry.id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422
        assert "HX-Trigger" in resp.headers
