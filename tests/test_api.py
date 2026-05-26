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


class TestCreateJournalsBatch:
    def test_success_multiple_entries(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [
                {
                    "date": "2026-02-01", "description": "ランチ",
                    "lines": [
                        {"account_code": accounts["5010"].code, "debit": 800},
                        {"account_code": accounts["1010"].code, "credit": 800},
                    ],
                },
                {
                    "date": "2026-02-02", "description": "コーヒー",
                    "lines": [
                        {"account_code": accounts["5010"].code, "debit": 500},
                        {"account_code": accounts["1010"].code, "credit": 500},
                    ],
                },
            ],
        })
        assert resp.status_code == 201, resp.get_json()
        data = resp.get_json()
        assert data["ok"] is True
        assert data["created_count"] == 2
        assert len(data["entries"]) == 2
        assert "batch_id" in data
        # 全 entry が同じ batch_id で DB に入っていること
        entries_db = JournalEntry.query.filter_by(user_id=user.id).all()
        assert {e.batch_id for e in entries_db} == {data["batch_id"]}

    def test_client_supplied_batch_id(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "batch_id": "my-import-2026-02",
            "entries": [{
                "date": "2026-02-15", "description": "test",
                "lines": [
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ],
            }],
        })
        assert resp.status_code == 201
        assert resp.get_json()["batch_id"] == "my-import-2026-02"

    def test_empty_entries_rejected(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [],
        })
        assert resp.status_code == 400

    def test_too_many_entries(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [
                {
                    "date": "2026-02-01", "description": f"e{i}",
                    "lines": [
                        {"account_code": accounts["5010"].code, "debit": 1},
                        {"account_code": accounts["1010"].code, "credit": 1},
                    ],
                } for i in range(501)
            ],
        })
        assert resp.status_code == 400
        assert "上限" in resp.get_json()["error"]

    def test_atomicity_partial_failure_rolls_back(
            self, client, db, user, accounts, auth_header):
        """1 entry でも貸借不一致なら、全 entry が rollback される。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [
                {
                    "date": "2026-02-01", "description": "valid",
                    "lines": [
                        {"account_code": accounts["5010"].code, "debit": 100},
                        {"account_code": accounts["1010"].code, "credit": 100},
                    ],
                },
                {
                    # 貸借不一致: create_journal_entry が ValueError
                    "date": "2026-02-02", "description": "invalid",
                    "lines": [
                        {"account_code": accounts["5010"].code, "debit": 100},
                        {"account_code": accounts["1010"].code, "credit": 99},
                    ],
                },
            ],
        })
        assert resp.status_code == 400
        # valid 側も保存されていない (rollback)
        assert JournalEntry.query.filter_by(user_id=user.id).count() == 0

    def test_invalid_date_format(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "date": "not-a-date", "description": "x",
                "lines": [
                    {"account_code": accounts["5010"].code, "debit": 1},
                    {"account_code": accounts["1010"].code, "credit": 1},
                ],
            }],
        })
        assert resp.status_code == 400

    def test_missing_description(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "date": "2026-02-01",
                "lines": [
                    {"account_code": accounts["5010"].code, "debit": 1},
                    {"account_code": accounts["1010"].code, "credit": 1},
                ],
            }],
        })
        assert resp.status_code == 400

    def test_scope_required(self, client, db, user, accounts):
        """journals:read のみのキーでは batch 起票不可。"""
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="readonly",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:read", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.post("/api/v1/journals/batch",
                           headers=_auth_header(raw_key), json={
            "entries": [{
                "date": "2026-02-01", "description": "x",
                "lines": [
                    {"account_code": accounts["5010"].code, "debit": 1},
                    {"account_code": accounts["1010"].code, "credit": 1},
                ],
            }],
        })
        assert resp.status_code == 403

    def test_no_auth(self, client, db, user, accounts):
        resp = client.post("/api/v1/journals/batch", json={"entries": []})
        assert resp.status_code == 401

    def test_fiscal_period_16_rejected(self, client, db, user, accounts, auth_header):
        """fp=16 (損益振替) は自動生成専用なので batch から起票できない。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "date": "2026-02-01", "description": "損益振替",
                "fiscal_period": 16,
                "lines": [
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ],
            }],
        })
        assert resp.status_code == 400
        assert "損益振替" in resp.get_json()["error"]

    def test_invalid_source_rejected(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "date": "2026-02-01", "description": "x", "source": "malicious",
                "lines": [
                    {"account_code": accounts["5010"].code, "debit": 1},
                    {"account_code": accounts["1010"].code, "credit": 1},
                ],
            }],
        })
        assert resp.status_code == 400
        assert "source" in resp.get_json()["error"]

    def test_description_too_long_rejected(self, client, db, user, accounts, auth_header):
        """description が 256 文字以上だと DB エラー (500) になるので 400 で弾く。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "date": "2026-02-01", "description": "x" * 256,
                "lines": [
                    {"account_code": accounts["5010"].code, "debit": 1},
                    {"account_code": accounts["1010"].code, "credit": 1},
                ],
            }],
        })
        assert resp.status_code == 400
        assert "255" in resp.get_json()["error"]

    def test_invalid_account_code_rejected(self, client, db, user, accounts, auth_header):
        """存在しない account_code は FK 違反 (500) ではなく 400 で返す。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "date": "2026-02-01", "description": "x",
                "lines": [
                    {"account_code": "9999", "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ],
            }],
        })
        assert resp.status_code == 400
        assert "9999" in resp.get_json()["error"]

    def test_float_amount_rejected(self, client, db, user, accounts, auth_header):
        """float の debit/credit は切り捨てで貸借不一致を隠すので拒否する。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "date": "2026-02-01", "description": "x",
                "lines": [
                    {"account_code": accounts["5010"].code, "debit": 100.5},
                    {"account_code": accounts["1010"].code, "credit": 100.5},
                ],
            }],
        })
        assert resp.status_code == 400
        assert "整数" in resp.get_json()["error"]

    def test_bool_amount_rejected(self, client, db, user, accounts, auth_header):
        """bool は int サブクラスなので明示的に弾く (True→1 の意図しない仕訳化防止)。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "date": "2026-02-01", "description": "x",
                "lines": [
                    {"account_code": accounts["5010"].code, "debit": True},
                    {"account_code": accounts["1010"].code, "credit": True},
                ],
            }],
        })
        assert resp.status_code == 400
        assert "整数" in resp.get_json()["error"]


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


# --- 医療費 (Phase E3-C-8b) ---


class TestMedicalExpenses:
    """GET /api/v1/medical-expenses"""

    def _make_expense(self, db, user_id, entry, **kwargs):
        from app.models.medical import MedicalExpense
        from datetime import date as _date
        e = MedicalExpense(
            user_id=user_id,
            journal_entry_id=entry.id,
            date=kwargs.get("date", _date(2026, 5, 1)),
            patient_name=kwargs.get("patient_name", "本人"),
            hospital_name=kwargs.get("hospital_name", "A病院"),
            treatment_description=kwargs.get("treatment_description", ""),
            provider_type=kwargs.get("provider_type", "hospital"),
            amount_paid=kwargs.get("amount_paid", 5000),
            insurance_reimbursement=kwargs.get("insurance_reimbursement", 0),
        )
        db.session.add(e)
        db.session.commit()
        return e

    def test_unauthenticated(self, client):
        resp = client.get("/api/v1/medical-expenses")
        assert resp.status_code in (302, 401)

    def test_empty_returns_total_0(self, client, db, user, accounts, auth_header):
        resp = client.get("/api/v1/medical-expenses", headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["expenses"] == []
        assert body["total"] == 0

    def test_lists_expenses_with_plaintext_fields(
        self, client, db, user, accounts, auth_header,
    ):
        entry = make_journal(db, user.id, "5010", "1010", 5000,
                             entry_date=date(2026, 5, 1))
        # tax_category=medical の科目を 5099 として用意するのが面倒なので
        # 既存科目で代用 (テストは MedicalExpense の取得経路を見る)
        e = self._make_expense(db, user.id, entry)
        resp = client.get("/api/v1/medical-expenses", headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total"] == 1
        item = body["expenses"][0]
        assert item["id"] == e.id
        assert item["journal_entry_id"] == entry.id
        assert item["patient_name"] == "本人"
        assert item["amount_paid"] == 5000
        # 暗号化未設定なら null
        assert item["encrypted_blob"] is None
        assert item["blob_iv"] is None

    def test_returns_blob_iv_base64(
        self, client, db, user, accounts, auth_header,
    ):
        from base64 import b64decode
        entry = make_journal(db, user.id, "5010", "1010", 5000,
                             entry_date=date(2026, 5, 1))
        e = self._make_expense(db, user.id, entry)
        e.encrypted_blob = b"\xAA" * 48
        e.blob_iv = b"\xBB" * 12
        db.session.commit()
        resp = client.get("/api/v1/medical-expenses", headers=auth_header)
        item = resp.get_json()["expenses"][0]
        assert b64decode(item["encrypted_blob"]) == b"\xAA" * 48
        assert b64decode(item["blob_iv"]) == b"\xBB" * 12

    def test_fiscal_year_filter(
        self, client, db, user, accounts, auth_header,
    ):
        entry2025 = make_journal(db, user.id, "5010", "1010", 1000,
                                 entry_date=date(2025, 6, 1))
        entry2026 = make_journal(db, user.id, "5010", "1010", 2000,
                                 entry_date=date(2026, 6, 1))
        self._make_expense(db, user.id, entry2025,
                           date=date(2025, 6, 1), amount_paid=1000)
        self._make_expense(db, user.id, entry2026,
                           date=date(2026, 6, 1), amount_paid=2000)
        # 2026 だけ取得
        resp = client.get("/api/v1/medical-expenses?fiscal_year=2026",
                          headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 1
        assert body["expenses"][0]["amount_paid"] == 2000

    def test_fiscal_year_invalid_int(
        self, client, db, user, accounts, auth_header,
    ):
        resp = client.get("/api/v1/medical-expenses?fiscal_year=abc",
                          headers=auth_header)
        assert resp.status_code == 400

    def test_fiscal_year_out_of_range(
        self, client, db, user, accounts, auth_header,
    ):
        resp = client.get("/api/v1/medical-expenses?fiscal_year=99999",
                          headers=auth_header)
        assert resp.status_code == 400

    def test_idor_only_own_user(
        self, client, db, user, auditor, accounts, auth_header,
    ):
        # auditor 用の Account を作る必要を避けるため、journal_entry_id=null
        # で直接 MedicalExpense を作成 (本人 user 用 entry はなし)。
        # 重要なのは別 user_id の expense が本人取得に出ないこと。
        from app.models.medical import MedicalExpense
        from datetime import date as _date
        other_exp = MedicalExpense(
            user_id=auditor.id,
            journal_entry_id=None,
            date=_date(2026, 6, 1),
            patient_name="他人",
            hospital_name="X病院",
            amount_paid=999,
        )
        db.session.add(other_exp)
        db.session.commit()
        # 本人 (user) は他人の expense を見えない
        resp = client.get("/api/v1/medical-expenses", headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 0


# --- 残高キャッシュ blob (E3-E-1) ---


class TestBalanceCacheBlobs:
    def _put(self, client, headers, year, period, blob_b64="aGVsbG8=", iv_b64=None):
        # AES-GCM IV は 12 byte。"AAAAAAAAAAAAAAAA" = 12 zero bytes の base64
        import base64
        iv_b64 = iv_b64 or base64.b64encode(b"\x00" * 12).decode()
        return client.put(
            f"/api/v1/balance-cache-blobs/{year}/{period}",
            headers=headers,
            json={"encrypted_blob": blob_b64, "blob_iv": iv_b64},
        )

    def test_put_creates_new_blob(self, client, db, user, accounts, auth_header):
        resp = self._put(client, auth_header, 2026, 3)
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data["ok"] is True
        assert data["updated_at"] is not None
        # ISO 8601 形式である
        assert "T" in data["updated_at"]

    def test_put_upserts_existing(self, client, db, user, accounts, auth_header):
        import base64
        self._put(client, auth_header, 2026, 3)
        r2 = self._put(
            client, auth_header, 2026, 3,
            blob_b64=base64.b64encode(b"updated").decode(),
        )
        assert r2.status_code == 200
        from app.models.balance_cache import BalanceCacheBlob
        rows = BalanceCacheBlob.query.filter_by(
            user_id=user.id, year=2026, period=3,
        ).all()
        assert len(rows) == 1
        assert rows[0].encrypted_blob == b"updated"

    def test_get_list_filters_by_year(self, client, db, user, accounts, auth_header):
        self._put(client, auth_header, 2026, 3)
        self._put(client, auth_header, 2026, 12)
        self._put(client, auth_header, 2025, 12)
        resp = client.get(
            "/api/v1/balance-cache-blobs?year=2026", headers=auth_header,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["blobs"]) == 2
        # period 順
        assert [b["period"] for b in data["blobs"]] == [3, 12]
        # 必要フィールドが揃っている
        for b in data["blobs"]:
            assert "encrypted_blob" in b
            assert "blob_iv" in b
            assert "year" in b
            assert "period" in b

    def test_get_requires_year(self, client, db, user, accounts, auth_header):
        resp = client.get("/api/v1/balance-cache-blobs", headers=auth_header)
        assert resp.status_code == 400

    def test_put_period_validation(self, client, db, user, accounts, auth_header):
        # period=17 (16=損益振替済まで許容、17 は範囲外) → 400
        resp = self._put(client, auth_header, 2026, 17)
        assert resp.status_code == 400

    def test_put_year_out_of_range(self, client, db, user, accounts, auth_header):
        resp = self._put(client, auth_header, 1800, 3)
        assert resp.status_code == 400

    def test_put_invalid_iv_length(self, client, db, user, accounts, auth_header):
        import base64
        # 11 byte IV (12 必須) → 400
        resp = self._put(
            client, auth_header, 2026, 3,
            iv_b64=base64.b64encode(b"\x00" * 11).decode(),
        )
        assert resp.status_code == 400

    def test_put_blob_too_large(self, client, db, user, accounts, auth_header):
        import base64
        # 33KB blob → 400 (上限 32KB)
        big = base64.b64encode(b"\x00" * (33 * 1024)).decode()
        resp = self._put(client, auth_header, 2026, 3, blob_b64=big)
        assert resp.status_code == 400
        # エラーメッセージに 32KB の上限が示されている (record 4KB ではない)
        err = resp.get_json()["error"]
        assert str(32 * 1024) in err

    def test_put_blob_just_under_limit_passes(self, client, db, user, accounts, auth_header):
        """30KB blob は通る (32KB 上限の確認)。"""
        import base64
        ok_size = base64.b64encode(b"\x00" * (30 * 1024)).decode()
        resp = self._put(client, auth_header, 2026, 3, blob_b64=ok_size)
        assert resp.status_code == 200, resp.get_json()

    def test_delete_year(self, client, db, user, accounts, auth_header):
        self._put(client, auth_header, 2026, 3)
        self._put(client, auth_header, 2026, 12)
        resp = client.delete(
            "/api/v1/balance-cache-blobs/2026", headers=auth_header,
        )
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == 2

    def test_delete_from_period(self, client, db, user, accounts, auth_header):
        self._put(client, auth_header, 2026, 3)
        self._put(client, auth_header, 2026, 12)
        # from_period=6 → 12 だけ削除 (3 は残る)
        resp = client.delete(
            "/api/v1/balance-cache-blobs/2026?from_period=6",
            headers=auth_header,
        )
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == 1
        from app.models.balance_cache import BalanceCacheBlob
        remaining = BalanceCacheBlob.query.filter_by(
            user_id=user.id, year=2026,
        ).all()
        assert len(remaining) == 1
        assert remaining[0].period == 3

    def _make_other_user_with_blob(self, db, year=2026, period=3, blob=b"other"):
        from app.models.balance_cache import BalanceCacheBlob
        from app.models.user import User
        from werkzeug.security import generate_password_hash
        u2 = User(
            username="other", email="o@example.com",
            password_hash=generate_password_hash("pw"),
        )
        db.session.add(u2); db.session.commit()
        db.session.add(BalanceCacheBlob(
            user_id=u2.id, year=year, period=period,
            encrypted_blob=blob, blob_iv=b"\x00" * 12,
        ))
        db.session.commit()
        return u2

    def test_idor_other_user_invisible(self, client, db, user, accounts, auth_header):
        """別ユーザーの blob は GET で見えない。"""
        self._make_other_user_with_blob(db)
        resp = client.get(
            "/api/v1/balance-cache-blobs?year=2026", headers=auth_header,
        )
        assert resp.status_code == 200
        assert resp.get_json()["blobs"] == []

    def test_idor_put_does_not_overwrite_other_user(
            self, client, db, user, accounts, auth_header):
        """別ユーザーの (year, period) を PUT しても、その人の blob は壊れない。"""
        from app.models.balance_cache import BalanceCacheBlob
        u2 = self._make_other_user_with_blob(db, blob=b"u2original")
        # user の auth_header で同 (year, period) に PUT
        self._put(client, auth_header, 2026, 3)
        # u2 の blob は残っている
        u2_blob = BalanceCacheBlob.query.filter_by(
            user_id=u2.id, year=2026, period=3,
        ).first()
        assert u2_blob is not None
        assert u2_blob.encrypted_blob == b"u2original"

    def test_idor_delete_does_not_delete_other_user(
            self, client, db, user, accounts, auth_header):
        """他ユーザーの blob は DELETE で消えない。"""
        from app.models.balance_cache import BalanceCacheBlob
        u2 = self._make_other_user_with_blob(db)
        resp = client.delete(
            "/api/v1/balance-cache-blobs/2026", headers=auth_header,
        )
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == 0
        u2_blob = BalanceCacheBlob.query.filter_by(user_id=u2.id).first()
        assert u2_blob is not None

    def test_delete_requires_delete_scope(self, client, db, user, accounts):
        """journals:create だけのキーでは DELETE 不可 (delete scope 必須)。"""
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="create-only",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:create", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.delete(
            "/api/v1/balance-cache-blobs/2026", headers=_auth_header(raw_key),
        )
        assert resp.status_code == 403

    def test_no_auth(self, client, db, user, accounts):
        resp = client.get("/api/v1/balance-cache-blobs?year=2026")
        assert resp.status_code == 401

    def test_put_requires_write_scope(self, client, db, user, accounts):
        # journals:read だけのキーでは PUT 不可
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="readonly",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:read", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        resp = self._put(client, _auth_header(raw_key), 2026, 3)
        assert resp.status_code == 403


# --- 全データバックアップ (v5 BU-1) ---


class TestBackupExport:
    def test_unauthenticated_rejected(self, client):
        resp = client.get("/api/v1/backup/export")
        assert resp.status_code == 401

    def test_empty_user_returns_skeleton(self, client, db, user, auth_header):
        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data["version"] == "1.0"
        assert data["user_id"] == user.id
        assert "exported_at" in data
        # 空ユーザでも data dict のキーは全部揃う (UI 側で undefined アクセス防止)
        assert set(data["data"].keys()) >= {
            "accounts", "fiscal_closes", "journal_entries",
            "journal_entry_lines", "medical_expenses", "balance_cache_blobs",
        }

    def test_user_data_included(
        self, client, db, user, accounts, auth_header,
    ):
        from tests.conftest import make_journal
        from datetime import date as d
        make_journal(
            db, user.id, "5010", "1010", 1000,
            entry_date=d(2026, 2, 15), source="cashbook",
            description="食費",
        )
        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        # 自分のアカウントが含まれる
        assert any(a["code"] == "5010" for a in data["accounts"])
        # 仕訳が含まれる
        assert len(data["journal_entries"]) == 1
        assert data["journal_entries"][0]["description"] == "食費"
        # ライン (借方=5010, 貸方=1010) が両方とも含まれる
        codes = {l["account_code"] for l in data["journal_entry_lines"]}
        assert codes == {"5010", "1010"}

    def test_other_user_data_excluded(
        self, app, client, db, user, auth_header,
    ):
        """別ユーザーのデータが leak しないこと (IDOR 確認)。"""
        from app.models.user import User
        from app.models.fiscal import FiscalClose
        other = User(
            username="other_user_for_backup",
            email="other_backup@example.com",
            user_type="personal",
        )
        other.set_password("password123")
        db.session.add(other)
        db.session.flush()
        db.session.add(FiscalClose(user_id=other.id, year=2026, closed_period=5))
        db.session.commit()

        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        # 他人の fiscal_close が含まれていないこと
        assert all(
            fc["year"] != 2026 or fc["closed_period"] != 5
            for fc in data["fiscal_closes"]
        )

    def test_encrypted_blob_passthrough(
        self, client, db, user, auth_header,
    ):
        """encrypted_blob / blob_iv が base64 で含まれる (BCB 経由)。"""
        import base64
        from app.models.balance_cache import BalanceCacheBlob
        db.session.add(BalanceCacheBlob(
            user_id=user.id, year=2026, period=12,
            encrypted_blob=b"\x01\x02\x03",
            blob_iv=b"\x00" * 12,
        ))
        db.session.commit()
        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        blobs = resp.get_json()["data"]["balance_cache_blobs"]
        assert len(blobs) == 1
        assert blobs[0]["encrypted_blob"] == base64.b64encode(b"\x01\x02\x03").decode()


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
