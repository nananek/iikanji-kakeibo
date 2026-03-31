"""仕訳複写用 create-api のテスト"""

import json

from app.models.journal import JournalEntry


class TestCreateApi:
    """POST /journal/create-api"""

    def test_create_success(self, db, logged_in_client, user, accounts, account_types):
        resp = logged_in_client.post(
            "/journal/create-api",
            data=json.dumps({
                "date": "2026-01-15",
                "description": "複写テスト",
                "lines": [
                    {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0, "credit_amount": 1000},
                ],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["entry_number"] is not None
        entry = JournalEntry.query.filter_by(source="journal").first()
        assert entry is not None
        assert entry.description == "複写テスト"

    def test_create_missing_date(self, db, logged_in_client, user, accounts, account_types):
        resp = logged_in_client.post(
            "/journal/create-api",
            data=json.dumps({
                "date": "",
                "description": "テスト",
                "lines": [
                    {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0, "credit_amount": 1000},
                ],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_no_lines(self, db, logged_in_client, user, accounts, account_types):
        resp = logged_in_client.post(
            "/journal/create-api",
            data=json.dumps({
                "date": "2026-01-15",
                "description": "テスト",
                "lines": [],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_unbalanced(self, db, logged_in_client, user, accounts, account_types):
        resp = logged_in_client.post(
            "/journal/create-api",
            data=json.dumps({
                "date": "2026-01-15",
                "description": "テスト",
                "lines": [
                    {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0, "credit_amount": 500},
                ],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "貸借が一致しません" in resp.get_json()["error"]

    def test_create_invalid_date(self, db, logged_in_client, user, accounts, account_types):
        resp = logged_in_client.post(
            "/journal/create-api",
            data=json.dumps({
                "date": "invalid",
                "description": "テスト",
                "lines": [
                    {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0, "credit_amount": 1000},
                ],
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_unauthenticated(self, client, accounts, account_types):
        resp = client.post("/journal/create-api",
                          data=json.dumps({"date": "2026-01-15", "description": "test", "lines": []}),
                          content_type="application/json")
        assert resp.status_code in (302, 401)
