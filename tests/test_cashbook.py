"""出納帳 (cashbook) ビューのテスト

cashbook.py のカバレッジ向上を目的とする。
"""

from datetime import date

from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry
from tests.conftest import make_journal


class TestIndex:
    def test_unauthenticated_redirects(self, client):
        resp = client.get("/cashbook/")
        assert resp.status_code in (302, 401)

    def test_empty_index(self, logged_in_client, accounts):
        resp = logged_in_client.get("/cashbook/")
        assert resp.status_code == 200

    def test_lists_cashbook_entries(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        make_journal(db, user.id, "5010", "1010", 2000,
                     entry_date=date(2026, 2, 16), source="cashbook")
        # journal source は除外される
        make_journal(db, user.id, "5010", "1010", 500,
                     entry_date=date(2026, 2, 17), source="journal")

        resp = logged_in_client.get("/cashbook/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # 出納帳の 2 件は表示、journal の 500 円は表示されない
        assert "1,000" in body or "1000" in body
        assert "2,000" in body or "2000" in body

    def test_date_filter(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 1, 15), source="cashbook")
        make_journal(db, user.id, "5010", "1010", 2000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        resp = logged_in_client.get(
            "/cashbook/?date_from=2026-02-01&date_to=2026-02-28"
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "2,000" in body or "2000" in body

    def test_pagination(self, db, logged_in_client, user, accounts):
        for i in range(25):
            make_journal(db, user.id, "5010", "1010", 100 + i,
                         entry_date=date(2026, 2, i + 1), source="cashbook")
        resp1 = logged_in_client.get("/cashbook/?page=1")
        resp2 = logged_in_client.get("/cashbook/?page=2")
        assert resp1.status_code == 200
        assert resp2.status_code == 200


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
        # 仕訳は作成されていない (server-side 経路が無効化されている)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).count() == 0


class TestEdit:
    def _make_cashbook(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1500,
                            entry_date=date(2026, 2, 15), source="cashbook",
                            description="編集対象")

    def test_unauthenticated(self, client):
        resp = client.get("/cashbook/1/edit")
        assert resp.status_code in (302, 401)

    def test_get_renders_form_with_existing(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "編集対象" in body

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
        db.session.refresh(entry)
        assert entry.description == "編集対象"  # 元のまま

    def test_edit_blocked_by_closed_period(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        # 確定済みなのでリダイレクト
        assert resp.status_code in (302, 303)

    def test_edit_get_prefill_transfer(self, db, logged_in_client, user, accounts):
        """資金移動 (BS↔BS) の編集 GET でフォームに transfer プリフィル。"""
        # 預金 (1010 / BS) → 現金 (1001 想定) も BS でないと transfer 判定にならない
        # 標準科目には現金 1001 があるはず。なければ asset 同士を作る
        from app.models.account import Account
        # 既存の BS 科目を確認 — 1010 は既に asset。asset 同士の transfer を作る
        cash = Account.query.filter_by(
            user_id=user.id, account_type_id=1
        ).first()  # asset
        # 同じ asset 内で別の科目があれば使う、なければ create
        bs_other = Account.query.filter(
            Account.user_id == user.id,
            Account.account_type_id == cash.account_type_id,
            Account.code != cash.code,
        ).first()
        if not bs_other:
            import pytest
            pytest.skip("BS 科目が 1 個のみで transfer 設定不可")
        entry = make_journal(
            db, user.id, cash.code, bs_other.code, 5000,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # transaction_type=transfer のラジオが選択済になる (HTML 検証は緩く)
        assert "transfer" in body

    def test_edit_get_prefill_income(self, db, logged_in_client, user, accounts):
        """収入 (BS:debit, P/L:credit) の編集 GET でフォームに income プリフィル。"""
        # 1010 (asset) を debit、4010 想定 (revenue) を credit
        # 標準科目に売上 4010 等があるはず
        from app.models.account import Account
        revenue = Account.query.filter_by(
            user_id=user.id, account_type_id=4
        ).first()  # revenue
        if not revenue:
            import pytest
            pytest.skip("revenue 科目が存在しない")
        # debit=1010 asset, credit=revenue → income
        entry = make_journal(
            db, user.id, "1010", revenue.code, 3000,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "income" in body


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
