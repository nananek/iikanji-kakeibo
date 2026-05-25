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
                {"account_code": accounts["5010"].code, "debit": 3000},
                {"account_code": accounts["1010"].code, "credit": 3000},
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
            "lines": [{"account_code": "1010", "debit": 100}],
        })
        assert resp.status_code == 400
        assert "date" in resp.get_json()["error"]

    def test_missing_description(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "",
            "lines": [{"account_code": "1010", "debit": 100}],
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
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
        })
        assert resp.status_code == 400

    def test_unbalanced_entry(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "不正",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 3000},
                {"account_code": accounts["1010"].code, "credit": 2000},
            ],
        })
        assert resp.status_code == 400

    def test_invalid_draft_id(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "テスト",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
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
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
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
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
        })
        assert resp.status_code == 400


class TestCreateJournalE2EE:
    """Phase E3: encrypted_blob / blob_iv / fiscal_year 受け付けのテスト。"""

    def _b64(self, n_bytes):
        from base64 import b64encode
        return b64encode(b"\x42" * n_bytes).decode("ascii")

    def test_accepts_encrypted_blob_and_iv(
        self, client, db, user, accounts, auth_header,
    ):
        """blob/iv 両方指定で DB に保存される。"""
        from app.models.journal import JournalEntry
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "テスト",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100,
                 "encrypted_blob": self._b64(48), "blob_iv": self._b64(12)},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
            "encrypted_blob": self._b64(48),
            "blob_iv": self._b64(12),
            "fiscal_year": 2026,
        })
        assert resp.status_code == 201
        entry = JournalEntry.query.get(resp.get_json()["id"])
        assert entry.encrypted_blob == b"\x42" * 48
        assert entry.blob_iv == b"\x42" * 12
        assert entry.fiscal_year == 2026
        # line 1 にのみ blob あり
        lines_by_code = {l.account_code: l for l in entry.lines}
        assert lines_by_code["5010"].encrypted_blob == b"\x42" * 48
        assert lines_by_code["5010"].blob_iv == b"\x42" * 12
        assert lines_by_code["1010"].encrypted_blob is None

    def test_fiscal_year_defaults_to_date_year(
        self, client, db, user, accounts, auth_header,
    ):
        from app.models.journal import JournalEntry
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2024-08-15", "description": "x",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
        })
        assert resp.status_code == 201
        entry = JournalEntry.query.get(resp.get_json()["id"])
        assert entry.fiscal_year == 2024

    def test_blob_without_iv_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
            "encrypted_blob": self._b64(48),
            # blob_iv なし
        })
        assert resp.status_code == 400
        assert "同時に指定" in resp.get_json()["error"]

    def test_line_blob_without_iv_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        """line[i] レベルでも blob/iv ペアの整合性を要求。"""
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100,
                 "encrypted_blob": self._b64(48)},  # blob_iv 欠落
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
        })
        assert resp.status_code == 400
        body = resp.get_json()
        assert "lines[0]" in body["error"]
        assert "同時に指定" in body["error"]

    def test_fiscal_year_bool_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        """bool は int サブクラスだが fiscal_year としては不正。"""
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
            "fiscal_year": True,
        })
        assert resp.status_code == 400
        assert "fiscal_year" in resp.get_json()["error"]

    def test_fiscal_year_out_of_range_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        for bad_year in (1899, 2201, -1, 99999):
            resp = client.post("/api/v1/journals", headers=auth_header, json={
                "date": "2026-02-15", "description": "x",
                "lines": [
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ],
                "fiscal_year": bad_year,
            })
            assert resp.status_code == 400, f"year={bad_year} should be rejected"
            assert "範囲" in resp.get_json()["error"]

    def test_invalid_iv_length_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
            "encrypted_blob": self._b64(48),
            "blob_iv": self._b64(8),  # 12B 必須
        })
        assert resp.status_code == 400
        assert "12B" in resp.get_json()["error"]

    def test_oversized_blob_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
            "encrypted_blob": self._b64(5000),  # 4KB 上限超え
            "blob_iv": self._b64(12),
        })
        assert resp.status_code == 400
        assert "大きすぎ" in resp.get_json()["error"]

    def test_invalid_base64_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
            "encrypted_blob": "!!!not base64!!!",
            "blob_iv": self._b64(12),
        })
        assert resp.status_code == 400
        assert "base64" in resp.get_json()["error"]

    def test_get_returns_encrypted_blob_as_base64(
        self, client, db, user, accounts, auth_header,
    ):
        """GET レスポンスに encrypted_blob/blob_iv が base64 で含まれる。"""
        from base64 import b64decode
        # まず E2EE 形式で 1 件作成
        post_resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100,
                 "encrypted_blob": self._b64(32), "blob_iv": self._b64(12)},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
            "encrypted_blob": self._b64(48),
            "blob_iv": self._b64(12),
            "fiscal_year": 2026,
        })
        eid = post_resp.get_json()["id"]
        # GET (詳細 API は {"journal": {...}} 形式)
        resp = client.get(f"/api/v1/journals/{eid}", headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()["journal"]
        assert b64decode(body["encrypted_blob"]) == b"\x42" * 48
        assert b64decode(body["blob_iv"]) == b"\x42" * 12
        assert body["fiscal_year"] == 2026
        # blob なしの line は null
        lines_by_code = {l["account_code"]: l for l in body["lines"]}
        assert b64decode(lines_by_code["5010"]["encrypted_blob"]) == b"\x42" * 32
        assert lines_by_code["1010"]["encrypted_blob"] is None
        assert lines_by_code["1010"]["blob_iv"] is None


# --- 仕訳一覧 ---


class TestListJournals:
    def test_empty(self, client, db, user, accounts, auth_header):
        resp = client.get("/api/v1/journals", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["journals"] == []
        assert data["total"] == 0

    def test_with_entries(self, client, db, user, accounts, auth_header):
        make_journal(db, user.id, "5010", "1010",
                     1000, entry_date=date(2026, 2, 15))
        make_journal(db, user.id, "5010", "1010",
                     2000, entry_date=date(2026, 2, 16))
        resp = client.get("/api/v1/journals", headers=auth_header)
        data = resp.get_json()
        assert data["total"] == 2
        assert len(data["journals"]) == 2

    def test_amounts_returned_as_int(self, client, db, user, accounts, auth_header):
        """debit/credit は文字列ではなく整数で返す（クライアント互換性）"""
        make_journal(db, user.id, "5010", "1010",
                     1500, entry_date=date(2026, 2, 15))
        resp = client.get("/api/v1/journals", headers=auth_header)
        data = resp.get_json()
        line = data["journals"][0]["lines"][0]
        assert isinstance(line["debit"], int), \
            f"debit should be int, got {type(line['debit']).__name__}"
        assert isinstance(line["credit"], int), \
            f"credit should be int, got {type(line['credit']).__name__}"

    def test_date_filter(self, client, db, user, accounts, auth_header):
        make_journal(db, user.id, "5010", "1010",
                     1000, entry_date=date(2026, 1, 15))
        make_journal(db, user.id, "5010", "1010",
                     2000, entry_date=date(2026, 2, 15))
        resp = client.get("/api/v1/journals?date_from=2026-02-01",
                          headers=auth_header)
        data = resp.get_json()
        assert data["total"] == 1

    def test_fiscal_year_filter(self, client, db, user, accounts, auth_header):
        """Phase E3: fiscal_year パラメータで年度別取得 (date 暗号化後の代替)。"""
        make_journal(db, user.id, "5010", "1010", 100,
                     entry_date=date(2024, 5, 1))
        make_journal(db, user.id, "5010", "1010", 200,
                     entry_date=date(2025, 5, 1))
        make_journal(db, user.id, "5010", "1010", 300,
                     entry_date=date(2026, 5, 1))
        resp = client.get("/api/v1/journals?fiscal_year=2025",
                          headers=auth_header)
        data = resp.get_json()
        assert data["total"] == 1
        assert data["journals"][0]["fiscal_year"] == 2025

    def test_fiscal_year_filter_invalid_int(
        self, client, db, user, accounts, auth_header,
    ):
        resp = client.get("/api/v1/journals?fiscal_year=abc",
                          headers=auth_header)
        assert resp.status_code == 400
        assert "整数" in resp.get_json()["error"]

    def test_fiscal_year_filter_out_of_range(
        self, client, db, user, accounts, auth_header,
    ):
        resp = client.get("/api/v1/journals?fiscal_year=99999",
                          headers=auth_header)
        assert resp.status_code == 400
        assert "範囲" in resp.get_json()["error"]

    def test_list_via_session_cookie(
        self, db, logged_in_client, user, accounts,
    ):
        """E3-C-1b: ブラウザ Cookie 認証で GET /api/v1/journals できる。
        @auth_required (Bearer + session) への置き換えの主目的。"""
        make_journal(db, user.id, "5010", "1010", 100,
                     entry_date=date(2026, 5, 1))
        resp = logged_in_client.get("/api/v1/journals")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 1
        # 新フィールド (fiscal_period / line.id) が含まれる
        e = data["journals"][0]
        assert "fiscal_period" in e
        assert "fiscal_year" in e
        assert all("id" in l for l in e["lines"])

    def test_list_scope_required_for_api_key(
        self, client, db, user, accounts,
    ):
        """auth_required(scope=...) は API キー認証時に scope を要求する。
        journals:create scope だけの key で GET /journals → 403。"""
        from app.models.api_key import APIKey
        from tests.conftest import _auth_header
        raw, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="write-only",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:create", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.get("/api/v1/journals", headers=_auth_header(raw))
        assert resp.status_code == 403
        assert "journals:read" in resp.get_json()["error"]

    def test_pagination(self, client, db, user, accounts, auth_header):
        for i in range(5):
            make_journal(db, user.id, "5010", "1010",
                         100 * (i + 1), entry_date=date(2026, 1, i + 1))
        resp = client.get("/api/v1/journals?page=1&per_page=2",
                          headers=auth_header)
        data = resp.get_json()
        assert data["total"] == 5
        assert len(data["journals"]) == 2
        assert data["page"] == 1

    def test_user_isolation(self, client, db, user, accounts, auth_header, auditor):
        """他ユーザーの仕訳は見えない"""
        make_journal(db, user.id, "5010", "1010", 1000)

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
        entry = make_journal(db, user.id, "5010", "1010", 2000)
        resp = client.get(f"/api/v1/journals/{entry.id}", headers=auth_header)
        assert resp.status_code == 200
        j = resp.get_json()["journal"]
        assert j["id"] == entry.id
        assert len(j["lines"]) == 2

    def test_not_found(self, client, db, user, accounts, auth_header):
        resp = client.get("/api/v1/journals/99999", headers=auth_header)
        assert resp.status_code == 404

    def test_get_via_session_cookie(
        self, db, logged_in_client, user, accounts,
    ):
        """E3-C-1c: Cookie 認証で GET /api/v1/journals/<id> できる。"""
        entry = make_journal(db, user.id, "5010", "1010", 1500,
                             entry_date=date(2026, 4, 1))
        resp = logged_in_client.get(f"/api/v1/journals/{entry.id}")
        assert resp.status_code == 200
        j = resp.get_json()["journal"]
        assert j["id"] == entry.id
        # E3 で追加した新フィールドも返る
        assert "fiscal_period" in j
        assert "fiscal_year" in j

    def test_get_scope_required_for_api_key(
        self, client, db, user, accounts,
    ):
        """journals:create のみの API キーで GET /journals/<id> → 403。"""
        from app.models.api_key import APIKey
        from tests.conftest import _auth_header
        raw, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="write-only",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:create", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        entry = make_journal(db, user.id, "5010", "1010", 500)
        resp = client.get(f"/api/v1/journals/{entry.id}",
                          headers=_auth_header(raw))
        assert resp.status_code == 403


# --- 仕訳削除 ---


class TestDeleteJournal:
    def test_success(self, client, db, user, accounts, auth_header):
        entry = make_journal(db, user.id, "5010", "1010", 500)
        resp = client.delete(f"/api/v1/journals/{entry.id}", headers=auth_header)
        assert resp.status_code == 200
        assert JournalEntry.query.get(entry.id) is None

    def test_not_found(self, client, db, user, accounts, auth_header):
        resp = client.delete("/api/v1/journals/99999", headers=auth_header)
        assert resp.status_code == 404

    def test_locked_period(self, client, db, user, accounts, auth_header):
        entry = make_journal(db, user.id, "5010", "1010",
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
