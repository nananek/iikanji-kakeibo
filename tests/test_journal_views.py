"""仕訳帳ブラウザビュー (journal.py) のテスト

API テストは test_api.py / test_journal_create_api.py で網羅済み。
こちらはフォーム経由のフロー（new / edit / delete / bulk_delete / batches）を扱う。

E3-F PR-B2 以降、new / edit は GET 専用。フォーム送信は JS が
entries_builder.buildJournalEntry で暗号化して batch / PUT API に投げる経路に
移行した (test_api.py::TestCreateJournalsBatch / TestUpdateJournal で網羅)。
旧 POST テストは 405 期待 1 件に集約する。
"""

import json
from datetime import date

from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry
from tests.conftest import make_journal


class TestIndex:
    def test_unauthenticated(self, client):
        resp = client.get("/journal/")
        assert resp.status_code in (302, 401)

    def test_empty(self, logged_in_client, accounts):
        resp = logged_in_client.get("/journal/")
        assert resp.status_code == 200

    def test_with_entries(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="journal",
                     description="ABC")
        resp = logged_in_client.get("/journal/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "ABC" in body

    def test_search_filter(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="journal",
                     description="ファミマ")
        make_journal(db, user.id, "5010", "1010", 2000,
                     entry_date=date(2026, 2, 16), source="journal",
                     description="セブン")
        resp = logged_in_client.get("/journal/?search=ファミマ")
        body = resp.get_data(as_text=True)
        assert "ファミマ" in body
        assert "セブン" not in body

    def test_date_range_filter(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 1, 15), source="journal",
                     description="JAN")
        make_journal(db, user.id, "5010", "1010", 2000,
                     entry_date=date(2026, 2, 15), source="journal",
                     description="FEB")
        resp = logged_in_client.get(
            "/journal/?date_from=2026-02-01&date_to=2026-02-28"
        )
        body = resp.get_data(as_text=True)
        assert "FEB" in body
        assert "JAN" not in body


class TestNewGet:
    def test_unauthenticated(self, client):
        resp = client.get("/journal/new")
        assert resp.status_code in (302, 401)

    def test_get_renders_form(self, logged_in_client, accounts):
        resp = logged_in_client.get("/journal/new")
        assert resp.status_code == 200


class TestNewPostRejected:
    """E3-F PR-B2: journal.new は GET 専用。POST は 405 を返し、
    平文 POST 経路でデータが書込まれないことを保証する。"""

    def test_post_returns_405(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/journal/new", data={
            "date": "2026-02-15",
            "description": "平文 POST",
            "fiscal_period": "",
            "lines_json": json.dumps([
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 1000},
            ]),
        })
        assert resp.status_code == 405
        # 仕訳は作成されていない (server-side 経路が無効化されている)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="journal"
        ).count() == 0


class TestEdit:
    def _make_entry(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1000,
                            entry_date=date(2026, 2, 15),
                            source="journal", description="ORIG")

    def test_unauthenticated(self, client):
        resp = client.get("/journal/1/edit")
        assert resp.status_code in (302, 401)

    def test_404_for_nonexistent(self, logged_in_client, accounts):
        resp = logged_in_client.get("/journal/9999/edit")
        assert resp.status_code == 404

    def test_idor_other_user(self, db, logged_in_client, user, accounts,
                             second_user, second_user_accounts):
        other = make_journal(
            db, second_user.id, "5010", "1010", 100,
            entry_date=date(2026, 2, 15), source="journal",
        )
        resp = logged_in_client.get(f"/journal/{other.id}/edit")
        assert resp.status_code == 404

    def test_get_renders_form(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.get(f"/journal/{entry.id}/edit")
        assert resp.status_code == 200

    def test_post_returns_405(self, db, logged_in_client, user, accounts):
        """E3-F PR-B2: journal.edit は GET 専用。POST は 405 を返し、
        平文 POST 経路で既存仕訳が書き換えられないことを保証する。"""
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit", data={
            "date": "2026-02-20",
            "description": "更新後",
            "fiscal_period": "",
            "lines_json": json.dumps([
                {"account_code": "5010", "debit_amount": 2000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 2000},
            ]),
        })
        assert resp.status_code == 405
        db.session.refresh(entry)
        assert entry.description == "ORIG"  # 元のまま

    def test_edit_blocked_by_closed_period(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.get(f"/journal/{entry.id}/edit")
        # 確定済みなのでリダイレクト
        assert resp.status_code in (302, 303)


class TestDelete:
    def _make_entry(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1000,
                            entry_date=date(2026, 2, 15),
                            source="journal")

    def test_unauthenticated(self, client):
        resp = client.post("/journal/1/delete")
        assert resp.status_code in (302, 401)

    def test_delete_success(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        eid = entry.id
        resp = logged_in_client.post(f"/journal/{eid}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(JournalEntry, eid) is None

    def test_delete_hx(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(
            f"/journal/{entry.id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers

    def test_delete_ajax(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(
            f"/journal/{entry.id}/delete",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True

    def test_delete_404(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/9999/delete")
        assert resp.status_code == 404

    def test_idor_other_user(self, db, logged_in_client, user, accounts,
                             second_user, second_user_accounts):
        other = make_journal(
            db, second_user.id, "5010", "1010", 100,
            entry_date=date(2026, 2, 15), source="journal",
        )
        resp = logged_in_client.post(f"/journal/{other.id}/delete")
        assert resp.status_code == 404

    def test_delete_blocked_by_closed_period_redirect(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(f"/journal/{entry.id}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(JournalEntry, entry.id) is not None

    def test_delete_blocked_by_closed_period_hx(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(
            f"/journal/{entry.id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422
        assert "HX-Trigger" in resp.headers

    def test_delete_blocked_by_closed_period_ajax(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(
            f"/journal/{entry.id}/delete",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False


class TestBulkDelete:
    def test_unauthenticated(self, client):
        resp = client.post("/journal/bulk-delete")
        assert resp.status_code in (302, 401)

    def test_bulk_delete_success(self, db, logged_in_client, user, accounts):
        e1 = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 2, 15), source="journal")
        e2 = make_journal(db, user.id, "5010", "1010", 200,
                          entry_date=date(2026, 2, 16), source="journal")
        resp = logged_in_client.post("/journal/bulk-delete", data={
            "entry_ids": [e1.id, e2.id],
        })
        assert resp.status_code in (302, 303)
        assert db.session.get(JournalEntry, e1.id) is None
        assert db.session.get(JournalEntry, e2.id) is None

    def test_bulk_delete_no_selection(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/bulk-delete", data={})
        assert resp.status_code in (302, 303)

    def test_bulk_delete_skips_locked(self, db, logged_in_client, user, accounts):
        e1 = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 2, 15), source="journal")
        e2 = make_journal(db, user.id, "5010", "1010", 200,
                          entry_date=date(2026, 1, 15), source="journal")
        # 2026-01 を確定済みにする → e2 だけロック
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = logged_in_client.post("/journal/bulk-delete", data={
            "entry_ids": [e1.id, e2.id],
        })
        assert resp.status_code in (302, 303)
        # e1 は削除、e2 は残る
        assert db.session.get(JournalEntry, e1.id) is None
        assert db.session.get(JournalEntry, e2.id) is not None

    def test_bulk_delete_open_redirect_blocked(self, db, logged_in_client, user, accounts):
        e1 = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 2, 15), source="journal")
        resp = logged_in_client.post("/journal/bulk-delete", data={
            "entry_ids": [e1.id],
            "redirect_url": "https://evil.example.com/x",
        })
        # 外部 URL は journal.index に書き換えられる
        assert resp.status_code in (302, 303)
        assert "evil.example.com" not in resp.headers.get("Location", "")

    def test_bulk_delete_other_user_ignored(self, db, logged_in_client, user, accounts,
                                             second_user, second_user_accounts):
        other = make_journal(
            db, second_user.id, "5010", "1010", 100,
            entry_date=date(2026, 2, 15), source="journal",
        )
        resp = logged_in_client.post("/journal/bulk-delete", data={
            "entry_ids": [other.id],
        })
        assert resp.status_code in (302, 303)
        # 他人の仕訳は削除されない
        assert db.session.get(JournalEntry, other.id) is not None


class TestBatches:
    def test_unauthenticated(self, client):
        resp = client.get("/journal/batches")
        assert resp.status_code in (302, 401)

    def test_empty(self, logged_in_client, accounts):
        resp = logged_in_client.get("/journal/batches")
        assert resp.status_code == 200

    def test_with_batches(self, db, logged_in_client, user, accounts):
        # batch_id 付きの仕訳を作る
        from uuid import uuid4
        bid = str(uuid4())
        from app.models.journal import JournalEntry, JournalEntryLine
        e = JournalEntry(
            user_id=user.id, date=date(2026, 2, 15),
            entry_number=1, description="csv import",
            source="csv", batch_id=bid,
        )
        e.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=100, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=0, credit_amount=100),
        ]
        db.session.add(e)
        db.session.commit()
        resp = logged_in_client.get("/journal/batches")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert bid in body or "csv" in body
