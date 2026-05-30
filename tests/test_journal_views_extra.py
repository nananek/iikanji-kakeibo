"""仕訳帳ビューの追加テスト

test_journal_views.py で扱った index/new/delete/bulk_delete に加えて、
get_json / edit_api / suggest_categories / ai_suggest_categories /
delete_batch を網羅。

E3-F PR-B2 以降、フォーム POST (edit) は廃止 (test_journal_views.py で 405 を担保)。
更新の本流は PUT /api/v1/journals/<id> (test_api.py::TestUpdateJournal) と
モーダル経由の /journal/<id>/edit-api (TestEditApi、本ファイル) の 2 経路。
"""

from datetime import date
from unittest.mock import patch

from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from tests.conftest import make_journal


class TestGetJson:
    def test_unauthenticated(self, client):
        resp = client.get("/journal/1/json")
        assert resp.status_code in (302, 401)

    def test_404_for_nonexistent(self, logged_in_client, accounts):
        resp = logged_in_client.get("/journal/9999/json")
        assert resp.status_code == 404

    def test_returns_entry_data(self, db, logged_in_client, user, accounts):
        entry = make_journal(db, user.id, "5010", "1010", 1500,
                              entry_date=date(2026, 2, 15),
                              source="journal", description="JSON取得")
        resp = logged_in_client.get(f"/journal/{entry.id}/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == entry.id
        assert data["description"] == "JSON取得"
        assert data["date"] == "2026-02-15"
        assert len(data["lines"]) == 2
        assert data["lines"][0]["debit_amount"] in (0, 1500)

    def test_idor_other_user(self, db, logged_in_client, accounts,
                             second_user, second_user_accounts):
        other = make_journal(
            db, second_user.id, "5010", "1010", 100,
            entry_date=date(2026, 2, 15), source="journal",
        )
        resp = logged_in_client.get(f"/journal/{other.id}/json")
        assert resp.status_code == 404

    def test_readonly_for_locked_entry(self, db, logged_in_client, user, accounts):
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 1, 15), source="journal")
        # 2026-01 を確定
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = logged_in_client.get(f"/journal/{entry.id}/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["is_readonly"] is True


class TestEditApi:
    def _make_entry(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1000,
                            entry_date=date(2026, 2, 15),
                            source="journal", description="ORIG")

    def test_unauthenticated(self, client):
        resp = client.post("/journal/1/edit-api", json={})
        assert resp.status_code in (302, 401)

    def test_404(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/9999/edit-api", json={})
        assert resp.status_code == 404

    def test_idor(self, db, logged_in_client, accounts,
                  second_user, second_user_accounts):
        other = make_journal(
            db, second_user.id, "5010", "1010", 100,
            entry_date=date(2026, 2, 15), source="journal",
        )
        resp = logged_in_client.post(f"/journal/{other.id}/edit-api", json={
            "date": "2026-02-15", "description": "x",
            "lines": [{"account_code": "5010", "debit_amount": 100, "credit_amount": 0}],
        })
        assert resp.status_code == 404

    def test_no_body(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api",
                                      json=None,
                                      content_type="application/json")
        assert resp.status_code == 400

    def test_missing_required(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "", "description": "",
            "lines": [],
        })
        assert resp.status_code == 400

    def test_no_lines(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15", "description": "x", "lines": [],
        })
        assert resp.status_code == 400

    def test_unbalanced(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15",
            "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 500},
            ],
        })
        assert resp.status_code == 400
        assert "貸借" in resp.get_json()["error"]

    def test_success(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-20",
            "description": "更新済み",
            "lines": [
                {"account_code": "5010", "debit_amount": 3000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 3000},
            ],
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        db.session.refresh(entry)
        assert entry.description == "更新済み"
        assert entry.date == date(2026, 2, 20)

    def test_locked_entry_rejected(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400

    def test_fiscal_period_16_blocked(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15", "description": "x",
            "fiscal_period": "16",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400
        assert "損益振替" in resp.get_json()["error"]


class TestSuggestCategoriesRemoved:
    """非AI /journal/api/suggest-categories は E3-F PR-D-4 で廃止。

    平文 description/date 読取を撤去し、クライアントが復号済み仕訳から推定する
    (crypto/suggest_categories_classical.js)。POST すると 404 を返すことを担保。
    """

    def test_endpoint_returns_404(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": ["ファミマ"], "payment_account_code": "1010",
        })
        assert resp.status_code == 404


class TestAiSuggestCategoriesRemoved:
    """/journal/api/ai-suggest-categories は廃止。
    POST すると 404 を返すことを担保。クライアントが直接
    /api/v1/suggest-categories/prompt-context + 自己 LLM 呼出で実行する。"""

    def test_endpoint_returns_404(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/journal/api/ai-suggest-categories",
            json={"payment_account_code": "1010", "rows": [{"description": "x"}]},
        )
        assert resp.status_code == 404


class TestDeleteBatch:
    def test_unauthenticated(self, client):
        resp = client.post("/journal/batches/some-id/delete")
        assert resp.status_code in (302, 401)

    def test_unknown_batch(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/batches/nonexistent-id/delete")
        assert resp.status_code in (302, 303)

    def test_delete_batch_success(self, db, logged_in_client, user, accounts):
        from uuid import uuid4
        bid = str(uuid4())
        for i in range(3):
            e = JournalEntry(
                user_id=user.id, date=date(2026, 2, i + 1),
                entry_number=i + 1, description=f"row{i}",
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
        resp = logged_in_client.post(f"/journal/batches/{bid}/delete")
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(batch_id=bid).count() == 0

    def test_delete_batch_locked_entries_skipped(self, db, logged_in_client, user, accounts):
        from uuid import uuid4
        bid = str(uuid4())
        # 2026-01 ロック / 2026-02 オープン
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        for i, m in enumerate([1, 2]):
            e = JournalEntry(
                user_id=user.id, date=date(2026, m, 1),
                entry_number=i + 1, description=f"row{i}",
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
        resp = logged_in_client.post(f"/journal/batches/{bid}/delete")
        assert resp.status_code in (302, 303)
        # 1月分は残る、2月分は削除
        remaining = JournalEntry.query.filter_by(batch_id=bid).all()
        assert len(remaining) == 1
        assert remaining[0].date.month == 1
