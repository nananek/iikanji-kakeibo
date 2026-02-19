"""REST API (api.py) のテスト — 仕訳CRUD・認証・スコープ"""

import json
from datetime import date

import pytest

from app.models.api_key import APIKey
from app.models.journal import JournalEntry
from tests.conftest import _auth_header, make_journal


# --- 認証 ---


class TestAuth:
    def test_no_auth_header(self, client, db, user, accounts):
        resp = client.get("/api/v1/journals")
        assert resp.status_code == 401

    def test_invalid_key(self, client, db, user, accounts):
        resp = client.get("/api/v1/journals",
                          headers={"Authorization": "Bearer ik_invalid"})
        assert resp.status_code == 401

    def test_inactive_key(self, client, db, user, accounts):
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="inactive",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:read", is_active=False,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.get("/api/v1/journals",
                          headers=_auth_header(raw_key))
        assert resp.status_code == 401

    def test_scope_check(self, client, db, user, accounts):
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="readonly",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:read", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        # read OK
        resp = client.get("/api/v1/journals",
                          headers=_auth_header(raw_key))
        assert resp.status_code == 200
        # create NG
        resp = client.post("/api/v1/journals",
                           headers=_auth_header(raw_key),
                           json={"date": "2026-01-01", "description": "x", "lines": []})
        assert resp.status_code == 403


# --- 仕訳起票 ---


class TestCreateJournal:
    def test_success(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "食材購入",
            "lines": [
                {"account_id": accounts["5010"].id, "debit": 3000},
                {"account_id": accounts["1010"].id, "credit": 3000},
            ],
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["ok"] is True
        assert "id" in data
        assert "entry_number" in data

    def test_missing_date(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "description": "テスト",
            "lines": [{"account_id": 1, "debit": 100}],
        })
        assert resp.status_code == 400
        assert "date" in resp.get_json()["error"]

    def test_missing_description(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "",
            "lines": [{"account_id": 1, "debit": 100}],
        })
        assert resp.status_code == 400

    def test_missing_lines(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "テスト",
        })
        assert resp.status_code == 400

    def test_invalid_date_format(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026/02/15",
            "description": "テスト",
            "lines": [
                {"account_id": accounts["5010"].id, "debit": 100},
                {"account_id": accounts["1010"].id, "credit": 100},
            ],
        })
        assert resp.status_code == 400

    def test_unbalanced_entry(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "不正",
            "lines": [
                {"account_id": accounts["5010"].id, "debit": 3000},
                {"account_id": accounts["1010"].id, "credit": 2000},
            ],
        })
        assert resp.status_code == 400

    def test_invalid_draft_id(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "テスト",
            "lines": [
                {"account_id": accounts["5010"].id, "debit": 100},
                {"account_id": accounts["1010"].id, "credit": 100},
            ],
            "draft_id": "abc",
        })
        assert resp.status_code == 400
        assert "draft_id" in resp.get_json()["error"]

    def test_nonexistent_draft_id(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "テスト",
            "lines": [
                {"account_id": accounts["5010"].id, "debit": 100},
                {"account_id": accounts["1010"].id, "credit": 100},
            ],
            "draft_id": 99999,
        })
        assert resp.status_code == 400

    def test_locked_period(self, client, db, user, accounts, auth_header):
        from app.models.fiscal import FiscalClose
        fc = FiscalClose(user_id=user.id, year=2026, closed_period=2)
        db.session.add(fc)
        db.session.commit()
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "確定済み月",
            "lines": [
                {"account_id": accounts["5010"].id, "debit": 100},
                {"account_id": accounts["1010"].id, "credit": 100},
            ],
        })
        assert resp.status_code == 400


# --- 仕訳一覧 ---


class TestListJournals:
    def test_empty(self, client, db, user, accounts, auth_header):
        resp = client.get("/api/v1/journals", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["journals"] == []
        assert data["total"] == 0

    def test_with_entries(self, client, db, user, accounts, auth_header):
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 2, 15))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     2000, entry_date=date(2026, 2, 16))
        resp = client.get("/api/v1/journals", headers=auth_header)
        data = resp.get_json()
        assert data["total"] == 2
        assert len(data["journals"]) == 2

    def test_date_filter(self, client, db, user, accounts, auth_header):
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     1000, entry_date=date(2026, 1, 15))
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                     2000, entry_date=date(2026, 2, 15))
        resp = client.get("/api/v1/journals?date_from=2026-02-01",
                          headers=auth_header)
        data = resp.get_json()
        assert data["total"] == 1

    def test_pagination(self, client, db, user, accounts, auth_header):
        for i in range(5):
            make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                         100 * (i + 1), entry_date=date(2026, 1, i + 1))
        resp = client.get("/api/v1/journals?page=1&per_page=2",
                          headers=auth_header)
        data = resp.get_json()
        assert data["total"] == 5
        assert len(data["journals"]) == 2
        assert data["page"] == 1

    def test_user_isolation(self, client, db, user, accounts, auth_header, auditor):
        """他ユーザーの仕訳は見えない"""
        make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id, 1000)

        # auditor 用の API キーを作成
        raw_key2, key_hash2, key_prefix2 = APIKey.generate()
        key2 = APIKey(
            user_id=auditor.id, name="auditor-key",
            key_hash=key_hash2, key_prefix=key_prefix2,
            scopes="journals:read", is_active=True,
        )
        db.session.add(key2)
        db.session.commit()

        resp = client.get("/api/v1/journals",
                          headers=_auth_header(raw_key2))
        assert resp.get_json()["total"] == 0


# --- 仕訳詳細 ---


class TestGetJournal:
    def test_success(self, client, db, user, accounts, auth_header):
        entry = make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id, 2000)
        resp = client.get(f"/api/v1/journals/{entry.id}", headers=auth_header)
        assert resp.status_code == 200
        j = resp.get_json()["journal"]
        assert j["id"] == entry.id
        assert len(j["lines"]) == 2

    def test_not_found(self, client, db, user, accounts, auth_header):
        resp = client.get("/api/v1/journals/99999", headers=auth_header)
        assert resp.status_code == 404


# --- 仕訳削除 ---


class TestDeleteJournal:
    def test_success(self, client, db, user, accounts, auth_header):
        entry = make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id, 500)
        resp = client.delete(f"/api/v1/journals/{entry.id}", headers=auth_header)
        assert resp.status_code == 200
        assert JournalEntry.query.get(entry.id) is None

    def test_not_found(self, client, db, user, accounts, auth_header):
        resp = client.delete("/api/v1/journals/99999", headers=auth_header)
        assert resp.status_code == 404

    def test_locked_period(self, client, db, user, accounts, auth_header):
        entry = make_journal(db, user.id, accounts["5010"].id, accounts["1010"].id,
                             500, entry_date=date(2026, 1, 15))
        from app.models.fiscal import FiscalClose
        fc = FiscalClose(user_id=user.id, year=2026, closed_period=1)
        db.session.add(fc)
        db.session.commit()
        resp = client.delete(f"/api/v1/journals/{entry.id}", headers=auth_header)
        assert resp.status_code == 400


# --- 下書き一覧 ---


class TestDraftsAPI:
    def test_list_empty(self, client, db, user, accounts, auth_header):
        resp = client.get("/api/v1/ai/drafts", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["drafts"] == []
        assert data["total"] == 0
        assert "page" in data
        assert "per_page" in data

    def test_invalid_status(self, client, db, user, accounts, auth_header):
        resp = client.get("/api/v1/ai/drafts?status=invalid",
                          headers=auth_header)
        assert resp.status_code == 400

    def test_draft_not_found(self, client, db, user, accounts, auth_header):
        resp = client.get("/api/v1/ai/drafts/99999", headers=auth_header)
        assert resp.status_code == 404

    def test_delete_draft_not_found(self, client, db, user, accounts, auth_header):
        resp = client.delete("/api/v1/ai/drafts/99999", headers=auth_header)
        assert resp.status_code == 404
