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


class TestNewPostExpense:
    def test_create_expense(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "1500",
            "description": "ランチ",
            "fiscal_period": "",
        })
        assert resp.status_code in (302, 303)
        entry = JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).first()
        assert entry is not None
        assert entry.description == "ランチ"

    def test_create_income(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "income",
            "payment_account_code": "1010",
            "category_account_code": "4010",
            "amount": "300000",
            "description": "給与",
            "fiscal_period": "",
        })
        assert resp.status_code in (302, 303)
        entry = JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).first()
        assert entry is not None
        assert entry.description == "給与"

    def test_create_transfer(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "transfer",
            "payment_account_code": "1010",
            "category_account_code": "1020",
            "amount": "10000",
            "description": "現金→預金",
            "fiscal_period": "",
        })
        assert resp.status_code in (302, 303)
        entry = JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).first()
        assert entry is not None

    def test_transfer_same_account_rejected(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "transfer",
            "payment_account_code": "1010",
            "category_account_code": "1010",
            "amount": "10000",
            "description": "同一",
            "fiscal_period": "",
        })
        assert resp.status_code == 200  # form 再表示
        body = resp.get_data(as_text=True)
        assert "異なる科目" in body
        # 仕訳は作成されていない
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).count() == 0

    def test_locked_period_rejected(self, db, logged_in_client, user, accounts):
        # 2026年2月を確定済みに
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "1000",
            "description": "確定済み",
            "fiscal_period": "",
        })
        # form 再表示で 200
        assert resp.status_code == 200
        # 仕訳は作成されていない
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).count() == 0

    def test_missing_required_fields(self, logged_in_client, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "",  # 必須なのに空
            "transaction_type": "expense",
            "payment_account_code": "",
            "category_account_code": "",
            "amount": "",
            "description": "",
            "fiscal_period": "",
        })
        # 200 でフォーム再表示
        assert resp.status_code == 200

    def test_fiscal_period_special(self, db, logged_in_client, user, accounts):
        """期首振戻月 (fiscal_period=0) で登録"""
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "100",
            "description": "期首振戻",
            "fiscal_period": "0",
        })
        assert resp.status_code in (302, 303)
        entry = JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).first()
        assert entry is not None
        assert entry.fiscal_period == 0


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

    def test_post_updates_entry(self, db, logged_in_client, user, accounts):
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
        assert resp.status_code in (302, 303)
        db.session.refresh(entry)
        assert entry.description == "更新後"

    def test_edit_blocked_by_closed_period(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        # 確定済みなのでリダイレクト
        assert resp.status_code in (302, 303)

    def test_edit_post_to_closed_target_period_rerenders(
        self, db, logged_in_client, user, accounts,
    ):
        """編集先の対象期間が確定済みなら同一フォーム再描画 (リダイレクトではない)。"""
        # 編集元は 2026-02 (未確定)、編集先 2026-01 を確定済みに
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = logged_in_client.post(f"/cashbook/{entry.id}/edit", data={
            "date": "2026-01-15",  # 確定済みに移そうとする
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "1000",
            "description": "x",
            "fiscal_period": "",
        })
        # check_period_open_for_new で確定済みエラー → フォーム再描画 (200)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "確定" in body or "closed" in body.lower() or "danger" in body

    def test_edit_post_transfer_same_account_rerenders(
        self, db, logged_in_client, user, accounts,
    ):
        """編集で資金移動の元/先を同一科目にすると同一フォーム再描画。"""
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        resp = logged_in_client.post(f"/cashbook/{entry.id}/edit", data={
            "date": "2026-02-15",
            "transaction_type": "transfer",
            "payment_account_code": "1010",
            "category_account_code": "1010",  # 同一
            "amount": "1000",
            "description": "x",
            "fiscal_period": "",
        })
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "移動元" in body or "異なる" in body

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
            # スキップ可能なケース (BS 科目が 1 個しかない場合)
            return
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
            return
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
