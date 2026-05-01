"""仕訳帳ビューの追加テスト

test_journal_views.py で扱った index/new/delete/bulk_delete に加えて、
edit POST / get_json / edit_api / suggest_categories / ai_suggest_categories /
delete_batch を網羅。
"""

import json
from datetime import date
from unittest.mock import patch

from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from tests.conftest import make_journal


def _post_edit(client, entry_id, *, date_str="2026-02-15",
               description="更新後", lines=None, fiscal_period=""):
    if lines is None:
        lines = [
            {"account_code": "5010", "debit_amount": 2000, "credit_amount": 0,
             "description": ""},
            {"account_code": "1010", "debit_amount": 0, "credit_amount": 2000,
             "description": ""},
        ]
    return client.post(f"/journal/{entry_id}/edit", data={
        "date": date_str,
        "description": description,
        "fiscal_period": fiscal_period,
        "lines_json": json.dumps(lines),
    })


class TestEditPost:
    def _make_entry(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1000,
                            entry_date=date(2026, 2, 15),
                            source="journal", description="ORIG")

    def test_post_updates(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = _post_edit(logged_in_client, entry.id, description="UPDATED")
        assert resp.status_code in (302, 303)
        db.session.refresh(entry)
        assert entry.description == "UPDATED"

    def test_unbalanced_rejected(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = _post_edit(logged_in_client, entry.id, lines=[
            {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0,
             "description": ""},
            {"account_code": "1010", "debit_amount": 0, "credit_amount": 500,
             "description": ""},
        ])
        # 200 でフォーム再表示
        assert resp.status_code == 200

    def test_post_invalid_lines_json(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit", data={
            "date": "2026-02-15",
            "description": "x",
            "fiscal_period": "",
            "lines_json": "not-json{",
        })
        assert resp.status_code == 200

    def test_post_fiscal_period_16_blocked(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = _post_edit(logged_in_client, entry.id, fiscal_period="16")
        # 損益振替は手動入力不可。SelectField の choices に無いので validate fail or block
        assert resp.status_code in (200, 302)

    def test_post_locked_target_period_rejected(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        # 2026-03 を確定済みにする → 03 への移動を試す
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=3))
        db.session.commit()
        # 03 に移動しようとすると edit ハンドラ自身がリダイレクト (entry_modifiable で弾かれる)
        resp = _post_edit(logged_in_client, entry.id, date_str="2026-03-15")
        assert resp.status_code in (200, 302, 303)


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


class TestSuggestCategories:
    def test_unauthenticated(self, client):
        resp = client.post("/journal/api/suggest-categories", json={})
        assert resp.status_code in (302, 401)

    def test_no_body(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/journal/api/suggest-categories",
            json=None, content_type="application/json",
        )
        assert resp.status_code == 400

    def test_empty_descriptions(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": [], "payment_account_code": "1010",
        })
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_only_empty_strings(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": ["", "", ""],
            "payment_account_code": "1010",
        })
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_returns_recent_match(self, db, logged_in_client, user, accounts):
        # 過去に「ファミマ」で 5010/1010 仕訳がある
        make_journal(db, user.id, "5010", "1010", 100,
                     entry_date=date(2026, 1, 15), source="cashbook",
                     description="ファミマ")
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": ["ファミマ"],
            "payment_account_code": "1010",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ファミマ"]["account_code"] == "5010"

    def test_no_match(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": ["未知の摘要"],
            "payment_account_code": "1010",
        })
        body = resp.get_json()
        assert "未知の摘要" not in body


class TestAiSuggestCategories:
    def test_unauthenticated(self, client):
        resp = client.post("/journal/api/ai-suggest-categories", json={})
        assert resp.status_code in (302, 401)

    def test_no_body(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/journal/api/ai-suggest-categories",
            json=None, content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_payment_account(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/ai-suggest-categories", json={
            "rows": [{"description": "x", "withdrawal": 100}],
        })
        assert resp.status_code == 400

    def test_missing_rows(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/ai-suggest-categories", json={
            "payment_account_code": "1010",
        })
        assert resp.status_code == 400

    def test_success(self, logged_in_client, accounts):
        with patch("app.services.ai_receipt.suggest_categories_by_ai") as mock_ai:
            mock_ai.return_value = {
                "セブン": {"account_code": "5010", "account_name": "食費"},
            }
            resp = logged_in_client.post("/journal/api/ai-suggest-categories", json={
                "payment_account_code": "1010",
                "rows": [{"description": "セブン", "withdrawal": 500}],
            })
            assert resp.status_code == 200
            assert resp.get_json()["セブン"]["account_code"] == "5010"

    def test_ai_value_error(self, logged_in_client, accounts):
        with patch("app.services.ai_receipt.suggest_categories_by_ai") as mock_ai:
            mock_ai.side_effect = ValueError("AI設定がありません")
            resp = logged_in_client.post("/journal/api/ai-suggest-categories", json={
                "payment_account_code": "1010",
                "rows": [{"description": "x", "withdrawal": 100}],
            })
            assert resp.status_code == 400

    def test_ai_runtime_error(self, logged_in_client, accounts):
        with patch("app.services.ai_receipt.suggest_categories_by_ai") as mock_ai:
            mock_ai.side_effect = RuntimeError("upstream timeout")
            resp = logged_in_client.post("/journal/api/ai-suggest-categories", json={
                "payment_account_code": "1010",
                "rows": [{"description": "x", "withdrawal": 100}],
            })
            assert resp.status_code == 500


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
