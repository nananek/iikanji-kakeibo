"""REST API (api.py) のテスト — 仕訳CRUD・認証・スコープ"""

import json
from datetime import date

import pytest

from app.models.api_key import APIKey
from app.models.journal import JournalEntry
from tests.conftest import (
    _auth_header, encrypt_lines, encrypted_payload, make_journal,
)


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
    # E3-F PR-D-6-6: wire 平文除去後、POST /journals は平文 date/description/
    # source を受け取らず、fiscal_year / fiscal_month を必須メタとして要求する。
    def test_success(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_year": 2026, "fiscal_month": 2,
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 3000},
                {"account_code": accounts["1010"].code, "credit": 3000},
            ]),
            **encrypted_payload(),
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["ok"] is True
        assert "id" in data
        assert "entry_number" in data

    def test_missing_fiscal_year(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_month": 2,
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ]),
            **encrypted_payload(),
        })
        assert resp.status_code == 400
        assert "fiscal_year" in resp.get_json()["error"]

    def test_missing_fiscal_month(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_year": 2026,
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ]),
            **encrypted_payload(),
        })
        assert resp.status_code == 400
        assert "fiscal_month" in resp.get_json()["error"]

    def test_missing_lines(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_year": 2026, "fiscal_month": 2,
        })
        assert resp.status_code == 400

    def test_unbalanced_entry(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_year": 2026, "fiscal_month": 2,
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 3000},
                {"account_code": accounts["1010"].code, "credit": 2000},
            ]),
            **encrypted_payload(),
        })
        assert resp.status_code == 400
        assert "一致" in resp.get_json()["error"]

    def test_invalid_draft_id(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_year": 2026, "fiscal_month": 2,
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ]),
            **encrypted_payload(),
            "draft_id": "abc",
        })
        assert resp.status_code == 400
        assert "draft_id" in resp.get_json()["error"]

    def test_nonexistent_draft_id(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_year": 2026, "fiscal_month": 2,
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ]),
            **encrypted_payload(),
            "draft_id": 99999,
        })
        assert resp.status_code == 400

    def test_locked_period(self, client, db, user, accounts, auth_header):
        from app.models.fiscal import FiscalClose
        fc = FiscalClose(user_id=user.id, year=2026, closed_period=2)
        db.session.add(fc)
        db.session.commit()
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_year": 2026, "fiscal_month": 2,
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ]),
            **encrypted_payload(),
        })
        assert resp.status_code == 400
        assert "確定済み" in resp.get_json()["error"]


class TestCreateJournalsBatch:
    # E3-F PR-D-6-6: 各 entry は平文 date/description/source を持たず、
    # fiscal_year / fiscal_month を必須メタとして要求する。
    def test_success_multiple_entries(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [
                {
                    "fiscal_year": 2026, "fiscal_month": 2,
                    "lines": encrypt_lines([
                        {"account_code": accounts["5010"].code, "debit": 800},
                        {"account_code": accounts["1010"].code, "credit": 800},
                    ]),
                    **encrypted_payload(),
                },
                {
                    "fiscal_year": 2026, "fiscal_month": 2,
                    "lines": encrypt_lines([
                        {"account_code": accounts["5010"].code, "debit": 500},
                        {"account_code": accounts["1010"].code, "credit": 500},
                    ]),
                    **encrypted_payload(),
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
                "fiscal_year": 2026, "fiscal_month": 2,
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ]),
                **encrypted_payload(),
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
                    "fiscal_year": 2026, "fiscal_month": 2,
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
                    "fiscal_year": 2026, "fiscal_month": 2,
                    "lines": encrypt_lines([
                        {"account_code": accounts["5010"].code, "debit": 100},
                        {"account_code": accounts["1010"].code, "credit": 100},
                    ]),
                    **encrypted_payload(),
                },
                {
                    # 貸借不一致: create_journal_entry が ValueError
                    "fiscal_year": 2026, "fiscal_month": 2,
                    "lines": encrypt_lines([
                        {"account_code": accounts["5010"].code, "debit": 100},
                        {"account_code": accounts["1010"].code, "credit": 99},
                    ]),
                    **encrypted_payload(),
                },
            ],
        })
        assert resp.status_code == 400
        # valid 側も保存されていない (rollback)
        assert JournalEntry.query.filter_by(user_id=user.id).count() == 0

    def test_missing_fiscal_month(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026,
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 1},
                    {"account_code": accounts["1010"].code, "credit": 1},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 400
        assert "fiscal_month" in resp.get_json()["error"]

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

    def test_fiscal_month_16_rejected(self, client, db, user, accounts, auth_header):
        """fiscal_month=16 (損益振替) は自動生成専用なので batch から起票できない。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026, "fiscal_month": 16,
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 400
        assert "損益振替" in resp.get_json()["error"]

    def test_fiscal_month_zero_accepted(self, client, db, user, accounts, auth_header):
        """fiscal_month=0 (期首振戻) は batch から起票できる。

        手動入力可能な特殊期間 (0=期首振戻、13-15=決算整理) のうち最も使われる
        0 を batch API レベルで担保しておく。
        """
        from app.models.journal import JournalEntry
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026, "fiscal_month": 0,
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 201
        created_id = resp.get_json()["entries"][0]["id"]
        entry = db.session.get(JournalEntry, created_id)
        assert entry.fiscal_month == 0

    def test_invalid_account_code_rejected(self, client, db, user, accounts, auth_header):
        """存在しない account_code は FK 違反 (500) ではなく 400 で返す。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026, "fiscal_month": 2,
                "lines": encrypt_lines([
                    {"account_code": "9999", "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 400
        assert "9999" in resp.get_json()["error"]

    def test_float_amount_rejected(self, client, db, user, accounts, auth_header):
        """float の debit/credit は切り捨てで貸借不一致を隠すので拒否する。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026, "fiscal_month": 2,
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 100.5},
                    {"account_code": accounts["1010"].code, "credit": 100.5},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 400
        assert "整数" in resp.get_json()["error"]

    def test_bool_amount_rejected(self, client, db, user, accounts, auth_header):
        """bool は int サブクラスなので明示的に弾く (True→1 の意図しない仕訳化防止)。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026, "fiscal_month": 2,
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": True},
                    {"account_code": accounts["1010"].code, "credit": True},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 400
        assert "整数" in resp.get_json()["error"]


class TestCreateJournalsBatchDraftId:
    """E3-F PR-B3: batch API entry-level draft_id (AI 証憑下書き → Voucher 紐付け)。

    quick-accept クライアントが暗号化済 entry に draft_id を載せて投げる経路の
    サーバ側 contract をカバーする。
    """

    @staticmethod
    def _make_draft(db_sess, user_id, status="analyzed"):
        from app.models.ai_draft import AIDraft
        d = AIDraft(
            user_id=user_id,
            image_key=f"vouchers/{user_id}/test.jpg",
            image_mime="image/jpeg",
            file_hash="0" * 64,
            file_size=100,
            suggestions_json='[{"date": "2026-02-15", "entry_description": "x"}]',
            status=status,
        )
        db_sess.session.add(d)
        db_sess.session.commit()
        return d

    def test_draft_id_creates_voucher_and_deletes_draft(
        self, client, db, user, accounts, auth_header,
    ):
        from app.models.ai_draft import AIDraft
        from app.models.voucher import Voucher
        d = self._make_draft(db, user.id)
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026, "fiscal_month": 2,
                "draft_id": d.id,
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 500},
                    {"account_code": accounts["1010"].code, "credit": 500},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 201, resp.get_json()
        entry_id = resp.get_json()["entries"][0]["id"]
        # draft が削除され、Voucher が紐付いている
        assert db.session.get(AIDraft, d.id) is None
        voucher = Voucher.query.filter_by(journal_entry_id=entry_id).first()
        assert voucher is not None
        assert voucher.user_id == user.id

    def test_other_user_draft_id_rejected_and_rolls_back(
        self, client, db, user, accounts, auth_header,
    ):
        """他人の draft_id を指定 → 400 で entry も Voucher も作られない。"""
        from app.models.ai_draft import AIDraft
        from app.models.user import User
        from app.models.voucher import Voucher
        other = User(username="other_bx3", email="otherbx3@example.com")
        other.set_password("pass")
        db.session.add(other)
        db.session.commit()
        d = self._make_draft(db, other.id)
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026, "fiscal_month": 2,
                "draft_id": d.id,
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 400
        assert "draft_id" in resp.get_json()["error"]
        # 他人の draft はそのまま残る
        assert db.session.get(AIDraft, d.id) is not None
        # 自分の entry / voucher も作られていない
        assert JournalEntry.query.filter_by(user_id=user.id).count() == 0
        assert Voucher.query.filter_by(user_id=user.id).count() == 0

    def test_nonexistent_draft_id_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026, "fiscal_month": 2,
                "draft_id": 99999,
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 400
        assert JournalEntry.query.filter_by(user_id=user.id).count() == 0

    def test_done_draft_id_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        """status='done' / 'temp' の下書きは status='analyzed' フィルタで弾く。"""
        d = self._make_draft(db, user.id, status="done")
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026, "fiscal_month": 2,
                "draft_id": d.id,
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 400
        assert JournalEntry.query.filter_by(user_id=user.id).count() == 0

    def test_draft_id_non_int_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [{
                "fiscal_year": 2026, "fiscal_month": 2,
                "draft_id": "abc",
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ]),
                **encrypted_payload(),
            }],
        })
        assert resp.status_code == 400
        assert "draft_id" in resp.get_json()["error"]

    def test_partial_failure_rolls_back_draft_and_voucher(
        self, client, db, user, accounts, auth_header,
    ):
        """entry[0] が draft 付きで成功直前に entry[1] が validate 失敗 →
        Voucher / Draft の状態も巻き戻る。
        """
        from app.models.ai_draft import AIDraft
        from app.models.voucher import Voucher
        d = self._make_draft(db, user.id)
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [
                {
                    "fiscal_year": 2026, "fiscal_month": 2,
                    "draft_id": d.id,
                    "lines": encrypt_lines([
                        {"account_code": accounts["5010"].code, "debit": 100},
                        {"account_code": accounts["1010"].code, "credit": 100},
                    ]),
                    **encrypted_payload(),
                },
                {
                    # 貸借不一致で fail
                    "fiscal_year": 2026, "fiscal_month": 2,
                    "lines": encrypt_lines([
                        {"account_code": accounts["5010"].code, "debit": 100},
                        {"account_code": accounts["1010"].code, "credit": 99},
                    ]),
                    **encrypted_payload(),
                },
            ],
        })
        assert resp.status_code == 400
        # entry も Voucher も作られていない、draft は残ったまま
        assert JournalEntry.query.filter_by(user_id=user.id).count() == 0
        assert Voucher.query.filter_by(user_id=user.id).count() == 0
        assert db.session.get(AIDraft, d.id) is not None


class TestCreateJournalE2EE:
    """Phase E3: encrypted_blob / blob_iv / fiscal_year 受け付けのテスト。

    PR-C 以降は encrypted_blob / blob_iv は必須化された。本クラスは
    blob/iv が正しく DB に保存されること、および blob/iv ペアの整合性
    (同時指定 / IV 長 / size cap / base64) を検証する。
    """

    def _b64(self, n_bytes):
        from base64 import b64encode
        return b64encode(b"\x42" * n_bytes).decode("ascii")

    def test_accepts_encrypted_blob_and_iv(
        self, client, db, user, accounts, auth_header,
    ):
        """blob/iv 両方指定で DB に保存される。"""
        from app.models.journal import JournalEntry
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_year": 2026, "fiscal_month": 2,
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ]),
            **encrypted_payload(),
        })
        assert resp.status_code == 201
        entry = JournalEntry.query.get(resp.get_json()["id"])
        assert entry.encrypted_blob == b"\x42" * 48
        assert entry.blob_iv == b"\x42" * 12
        assert entry.fiscal_year == 2026
        # 全 line に blob あり (PR-C 以降必須)
        for line in entry.lines:
            assert line.encrypted_blob == b"\x42" * 48
            assert line.blob_iv == b"\x42" * 12

    def test_fiscal_meta_populated_and_returned(
        self, client, db, user, accounts, auth_header,
    ):
        """E3-F PR-D-6-6: POST で fiscal_year / fiscal_month / is_closing(False)
        が populate され、GET レスポンスにも含まれる。"""
        from app.models.journal import JournalEntry
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_year": 2026, "fiscal_month": 8,
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ]),
            **encrypted_payload(),
        })
        assert resp.status_code == 201
        eid = resp.get_json()["id"]
        entry = JournalEntry.query.get(eid)
        assert entry.is_closing is False
        assert entry.fiscal_year == 2026
        assert entry.fiscal_month == 8
        # GET レスポンスに保持列メタが含まれる。
        got = client.get(f"/api/v1/journals/{eid}", headers=auth_header)
        journal = got.get_json()["journal"]
        assert journal["is_closing"] is False
        assert journal["fiscal_month"] == 8

    def test_blob_without_iv_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        """entry レベルで blob のみ / iv のみは "同時に指定" 拒否。"""
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ]),
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
                # line[0]: blob あり / iv 欠落
                {"account_code": accounts["5010"].code, "debit": 100,
                 "encrypted_blob": self._b64(48)},
                # line[1]: 正常 (必須化に通すため)
                {"account_code": accounts["1010"].code, "credit": 100,
                 **encrypted_payload()},
            ],
            **encrypted_payload(),
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
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ]),
            **encrypted_payload(),
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
                "lines": encrypt_lines([
                    {"account_code": accounts["5010"].code, "debit": 100},
                    {"account_code": accounts["1010"].code, "credit": 100},
                ]),
                **encrypted_payload(),
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

    def test_plaintext_only_post_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        """PR-C: entry レベルの encrypted_blob/blob_iv が無い POST は 400 拒否。"""
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ]),
            # encrypted_blob / blob_iv 未指定
        })
        assert resp.status_code == 400
        body = resp.get_json()
        assert "encrypted_blob" in body["error"]
        assert "必須" in body["error"]

    def test_plaintext_line_in_otherwise_encrypted_entry_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        """PR-C: entry blob はあるが lines[i] に blob 欠落 → 400 拒否。"""
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                # line[0]: 暗号化済み
                {"account_code": accounts["5010"].code, "debit": 100,
                 **encrypted_payload()},
                # line[1]: 平文のみ
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
            **encrypted_payload(),
        })
        assert resp.status_code == 400
        body = resp.get_json()
        assert "lines[1]" in body["error"]
        assert "必須" in body["error"]

    def test_batch_plaintext_only_entry_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        """PR-C: batch 内の 1 entry でも encrypted_blob 欠落 → 400 (全 rollback)。"""
        resp = client.post("/api/v1/journals/batch", headers=auth_header, json={
            "entries": [
                {
                    "fiscal_year": 2026, "fiscal_month": 2,
                    "lines": encrypt_lines([
                        {"account_code": accounts["5010"].code, "debit": 100},
                        {"account_code": accounts["1010"].code, "credit": 100},
                    ]),
                    **encrypted_payload(),
                },
                {
                    # encrypted_blob 欠落の不正 entry
                    "fiscal_year": 2026, "fiscal_month": 2,
                    "lines": [
                        {"account_code": accounts["5010"].code, "debit": 100},
                        {"account_code": accounts["1010"].code, "credit": 100},
                    ],
                },
            ],
        })
        assert resp.status_code == 400
        body = resp.get_json()
        assert "entries[1]" in body["error"]
        assert "必須" in body["error"]
        # 全 rollback されている
        assert JournalEntry.query.filter_by(user_id=user.id).count() == 0

    def test_put_plaintext_only_update_rejected(
        self, client, db, user, accounts, auth_header,
    ):
        """PR-C: PUT も encrypted_blob 必須化対象。"""
        entry = make_journal(
            db, user.id, accounts["5010"].code, accounts["1010"].code, 100,
        )
        resp = client.put(f"/api/v1/journals/{entry.id}",
                          headers=auth_header,
                          json={
                              "date": "2026-03-15", "description": "x",
                              "lines": [
                                  {"account_code": accounts["5010"].code, "debit": 100},
                                  {"account_code": accounts["1010"].code, "credit": 100},
                              ],
                          })
        assert resp.status_code == 400
        assert "必須" in resp.get_json()["error"]

    def test_get_returns_encrypted_blob_as_base64(
        self, client, db, user, accounts, auth_header,
    ):
        """GET レスポンスに encrypted_blob/blob_iv が base64 で含まれる。"""
        from base64 import b64decode
        # まず E2EE 形式で 1 件作成 (PR-C 以降 全 line に blob 必須)
        post_resp = client.post("/api/v1/journals", headers=auth_header, json={
            "fiscal_year": 2026, "fiscal_month": 2,
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100,
                 "encrypted_blob": self._b64(32), "blob_iv": self._b64(12)},
                {"account_code": accounts["1010"].code, "credit": 100,
                 "encrypted_blob": self._b64(40), "blob_iv": self._b64(12)},
            ],
            "encrypted_blob": self._b64(48),
            "blob_iv": self._b64(12),
        })
        eid = post_resp.get_json()["id"]
        # GET (詳細 API は {"journal": {...}} 形式)
        resp = client.get(f"/api/v1/journals/{eid}", headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()["journal"]
        assert b64decode(body["encrypted_blob"]) == b"\x42" * 48
        assert b64decode(body["blob_iv"]) == b"\x42" * 12
        assert body["fiscal_year"] == 2026
        lines_by_code = {l["account_code"]: l for l in body["lines"]}
        assert b64decode(lines_by_code["5010"]["encrypted_blob"]) == b"\x42" * 32
        assert b64decode(lines_by_code["1010"]["encrypted_blob"]) == b"\x42" * 40


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

    # E3-F PR-D-6-3: date_from / date_to による絞り込みは撤去したため
    # 関連テスト (test_date_filter / test_date_*_invalid_format) を削除した。
    # 年度別取得は test_fiscal_year_filter が担保する。

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
        # E3-F PR-D-6-3b: 平文 date/description/source/fiscal_period は撤去済。
        # 保持列 fiscal_year/fiscal_month/is_closing + 暗号化 blob を返す。
        e = data["journals"][0]
        assert "fiscal_year" in e
        assert "fiscal_month" in e
        assert "is_closing" in e
        assert "date" not in e
        assert "description" not in e
        assert "source" not in e
        assert "fiscal_period" not in e
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


# --- インポートバッチ一覧 (E3-F PR-D-6-3b-2) ---


class TestListJournalBatches:
    """GET /api/v1/journals/batches (取込履歴のクライアント描画用)。

    batch_id でグルーピングし保持列メタ (件数 / 取込日時 / 削除可否) と各仕訳の
    encrypted_blob を返す。種別ラベル / 日付範囲は復号 blob からクライアントが
    導出するため、サーバ側テストはグルーピング・削除可否・blob 同梱を検証する。
    """

    def _make_batch(self, db, user, bid, dates, source="csv"):
        for d in dates:
            e = make_journal(db, user.id, "5010", "1010", 100,
                             entry_date=d, source=source)
            e.batch_id = bid
        db.session.commit()

    def test_unauthenticated(self, client, db, user, accounts):
        resp = client.get("/api/v1/journals/batches")
        assert resp.status_code == 401

    def test_empty(self, client, db, user, accounts, auth_header):
        resp = client.get("/api/v1/journals/batches", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["batches"] == []

    def test_groups_by_batch_id(self, client, db, user, accounts, auth_header):
        self._make_batch(db, user, "bid-csv",
                         [date(2026, 2, 3), date(2026, 2, 15)], source="csv")
        self._make_batch(db, user, "bid-web",
                         [date(2026, 3, 1)], source="web")
        resp = client.get("/api/v1/journals/batches", headers=auth_header)
        data = resp.get_json()
        assert len(data["batches"]) == 2
        by_id = {b["batch_id"]: b for b in data["batches"]}
        assert by_id["bid-csv"]["count"] == 2
        assert by_id["bid-web"]["count"] == 1
        # source / date 範囲はサーバ側では平文返却しない (blob のみ)。
        assert "source" not in by_id["bid-csv"]
        assert "date_from" not in by_id["bid-csv"]

    def test_entries_include_blob_fields(
        self, client, db, user, accounts, auth_header,
    ):
        self._make_batch(db, user, "bid-csv", [date(2026, 2, 3)])
        resp = client.get("/api/v1/journals/batches", headers=auth_header)
        b = resp.get_json()["batches"][0]
        assert b["entries"]
        e = b["entries"][0]
        assert "encrypted_blob" in e and "blob_iv" in e
        assert "fiscal_year" in e and "is_closing" in e
        # lines は取込一覧に不要なので含めない。
        assert "lines" not in e

    def test_excludes_entries_without_batch_id(
        self, client, db, user, accounts, auth_header,
    ):
        # batch_id なしの単発仕訳は取込履歴に出ない。
        make_journal(db, user.id, "5010", "1010", 100,
                     entry_date=date(2026, 2, 1))
        resp = client.get("/api/v1/journals/batches", headers=auth_header)
        assert resp.get_json()["batches"] == []

    def test_closing_batch_not_deletable(
        self, client, db, user, accounts, auth_header,
    ):
        self._make_batch(db, user, "bid-closing",
                         [date(2026, 12, 31)], source="closing")
        resp = client.get("/api/v1/journals/batches", headers=auth_header)
        b = resp.get_json()["batches"][0]
        assert b["is_closing"] is True
        assert b["deletable"] is False
        assert "損益振替" in b["delete_reason"]

    def test_locked_period_not_deletable(
        self, client, db, user, accounts, auth_header,
    ):
        from app.models.fiscal import FiscalClose
        # 2026-02 までロック (closed_period=2)。
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        self._make_batch(db, user, "bid-locked", [date(2026, 2, 10)])
        resp = client.get("/api/v1/journals/batches", headers=auth_header)
        b = resp.get_json()["batches"][0]
        assert b["deletable"] is False
        assert "確定済み" in b["delete_reason"]

    def test_open_period_deletable(
        self, client, db, user, accounts, auth_header,
    ):
        self._make_batch(db, user, "bid-open", [date(2026, 5, 1)])
        resp = client.get("/api/v1/journals/batches", headers=auth_header)
        b = resp.get_json()["batches"][0]
        assert b["deletable"] is True
        assert b["delete_reason"] == ""

    def test_via_session_cookie(self, db, logged_in_client, user, accounts):
        """ブラウザ Cookie 認証で取得できる (描画はブラウザ側)。"""
        self._make_batch(db, user, "bid-csv", [date(2026, 2, 3)])
        resp = logged_in_client.get("/api/v1/journals/batches")
        assert resp.status_code == 200
        assert resp.get_json()["batches"][0]["batch_id"] == "bid-csv"

    def test_user_isolation(
        self, client, db, user, accounts, auth_header, auditor,
    ):
        """他ユーザーのバッチは見えない。"""
        self._make_batch(db, user, "bid-mine", [date(2026, 2, 3)])
        raw_key2, key_hash2, key_prefix2 = APIKey.generate()
        key2 = APIKey(
            user_id=auditor.id, name="auditor-key",
            key_hash=key_hash2, key_prefix=key_prefix2,
            scopes="journals:read", is_active=True,
        )
        db.session.add(key2)
        db.session.commit()
        resp = client.get("/api/v1/journals/batches",
                          headers=_auth_header(raw_key2))
        assert resp.get_json()["batches"] == []


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
        # E3-F PR-D-6-3b: 平文 fiscal_period 等は撤去済、保持列のみ返る
        assert "fiscal_year" in j
        assert "fiscal_month" in j
        assert "is_closing" in j
        assert "fiscal_period" not in j
        assert "source" not in j

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

    def test_lists_expenses_blob_only(
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
        # E3-F PR-D-6-5-pre1: 平文列は返さない (client が encrypted_blob を復号)。
        for k in ("date", "patient_name", "hospital_name",
                  "treatment_description", "provider_type", "amount_paid",
                  "insurance_reimbursement"):
            assert k not in item
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
        # 2026 だけ取得 (fiscal_year フィルタは JE.fiscal_year ベース)
        resp = client.get("/api/v1/medical-expenses?fiscal_year=2026",
                          headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 1
        # E3-F PR-D-6-5-pre1: 平文 amount_paid は返さないので journal_entry_id で識別
        assert body["expenses"][0]["journal_entry_id"] == entry2026.id

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
        )
        db.session.add(other_exp)
        db.session.commit()
        # 本人 (user) は他人の expense を見えない
        resp = client.get("/api/v1/medical-expenses", headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 0


class TestMedicalExpensesUpsert:
    """POST /api/v1/medical-expenses (E3-F PR-D-3, 作成 or 更新)"""

    def _payload(self, entry_id, **kwargs):
        p = {
            "journal_entry_id": entry_id,
            "date": "2026-05-01",
            "patient_name": "本人",
            "hospital_name": "A病院",
            "treatment_description": "内科",
            "provider_type": "hospital",
            "amount_paid": 5000,
            "insurance_reimbursement": 1000,
            **encrypted_payload(),
        }
        p.update(kwargs)
        return p

    def test_unauthenticated(self, client):
        resp = client.post("/api/v1/medical-expenses", json={"journal_entry_id": 1})
        assert resp.status_code in (302, 401)

    def test_create_new(self, client, db, user, accounts, auth_header):
        from app.models.medical import MedicalExpense
        entry = make_journal(db, user.id, "5010", "1010", 5000,
                             entry_date=date(2026, 5, 1))
        resp = client.post("/api/v1/medical-expenses",
                           json=self._payload(entry.id), headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        me = MedicalExpense.query.filter_by(journal_entry_id=entry.id).first()
        assert me is not None
        assert me.id == body["id"]
        # E3-F PR-D-6-5: 平文列 (patient_name 等) は DROP 済。本体は
        # encrypted_blob のみに保存する。
        assert me.encrypted_blob is not None
        assert me.blob_iv is not None

    def test_update_existing_keeps_id(self, client, db, user, accounts, auth_header):
        from app.models.medical import MedicalExpense
        entry = make_journal(db, user.id, "5010", "1010", 5000,
                             entry_date=date(2026, 5, 1))
        r1 = client.post("/api/v1/medical-expenses",
                         json=self._payload(entry.id), headers=auth_header)
        me_id = r1.get_json()["id"]
        r2 = client.post("/api/v1/medical-expenses",
                         json=self._payload(entry.id, patient_name="更新後",
                                            insurance_reimbursement=200),
                         headers=auth_header)
        assert r2.status_code == 200
        assert r2.get_json()["id"] == me_id
        # E3-F PR-D-6-4: 同一 journal_entry_id は upsert で 1 行を維持 (id 不変)。
        # 平文列は書かないため値の比較ではなく行数・id の同一性で検証する。
        assert MedicalExpense.query.filter_by(journal_entry_id=entry.id).count() == 1
        me = db.session.get(MedicalExpense, me_id)
        db.session.refresh(me)
        assert me.encrypted_blob is not None

    def test_missing_blob_400(self, client, db, user, accounts, auth_header):
        entry = make_journal(db, user.id, "5010", "1010", 5000,
                             entry_date=date(2026, 5, 1))
        payload = self._payload(entry.id)
        del payload["encrypted_blob"]
        del payload["blob_iv"]
        resp = client.post("/api/v1/medical-expenses", json=payload,
                           headers=auth_header)
        assert resp.status_code == 400

    def test_journal_not_found_404(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/medical-expenses",
                           json=self._payload(999999), headers=auth_header)
        assert resp.status_code == 404

    def test_idor_other_user_journal_404(
        self, client, db, user, auditor, accounts, second_user,
        second_user_accounts, auth_header,
    ):
        other = make_journal(db, second_user.id, "5010", "1010", 100,
                             entry_date=date(2026, 5, 1))
        resp = client.post("/api/v1/medical-expenses",
                           json=self._payload(other.id), headers=auth_header)
        assert resp.status_code == 404

    def test_invalid_journal_entry_id_400(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/medical-expenses",
                           json=self._payload("abc"), headers=auth_header)
        assert resp.status_code == 400

    # E3-F PR-D-6-4: 平文フィールド (date / provider_type / amount_paid 等) の
    # 検証は撤去した (サーバは保存しないため)。旧 test_invalid_date_400 /
    # test_provider_type_non_string_400 / test_amount_non_int_400 を削除。
    # 不正値はクライアント側 (暗号化前) で検証する責務に移行。

    def test_closed_period_400(self, client, db, user, accounts, auth_header):
        from app.models.fiscal import FiscalClose
        entry = make_journal(db, user.id, "5010", "1010", 5000,
                             entry_date=date(2026, 5, 1))
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=5))
        db.session.commit()
        resp = client.post("/api/v1/medical-expenses",
                           json=self._payload(entry.id), headers=auth_header)
        assert resp.status_code == 400

    def test_empty_body_400(self, client, db, user, accounts, auth_header):
        resp = client.post("/api/v1/medical-expenses", json=None,
                           headers=auth_header)
        assert resp.status_code == 400

    # E3-F PR-D-6-5: provider_type 平文列は DROP 済 (サーバは保存も正規化も
    # しない)。旧 test_provider_type_empty_becomes_null を削除。


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
            "vouchers", "ai_drafts", "user_ai_config", "webhook_configs",
            "tax_form_mappings", "csv_column_profiles",
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
        e0 = data["journal_entries"][0]
        # E3-F PR-D-6-4: 平文 date/description/source/fiscal_period は export されない
        # (クライアントが encrypted_blob を復号して平文 JSON を組み立てる)。
        assert "description" not in e0
        assert "date" not in e0
        assert "source" not in e0
        assert "fiscal_period" not in e0
        # 年度フィルタ・closing 合成用の平文メタは残る
        assert e0["fiscal_year"] == 2026
        assert e0["fiscal_month"] == 2
        assert e0["is_closing"] is False
        # ライン (借方=5010, 貸方=1010) が両方とも含まれる。description は出ない。
        codes = {l["account_code"] for l in data["journal_entry_lines"]}
        assert codes == {"5010", "1010"}
        assert all("description" not in l for l in data["journal_entry_lines"])

    def test_other_user_data_excluded(
        self, app, client, db, user, accounts, auth_header,
    ):
        """別ユーザーのデータが leak しないこと (IDOR 確認)。
        全テーブルの user 分離を assertion で明示する (PR #243 review
        follow-up)。"""
        from datetime import date as d
        from app.models.account import Account, AccountType
        from app.models.user import User
        from app.models.fiscal import FiscalClose
        from app.models.journal import JournalEntry, JournalEntryLine
        from app.models.medical import MedicalExpense
        from app.models.balance_cache import BalanceCacheBlob

        other = User(
            username="other_user_for_backup",
            email="other_backup@example.com",
            user_type="personal",
        )
        other.set_password("password123")
        db.session.add(other)
        db.session.flush()
        # 他人の account / fiscal_close / journal / medical / BCB を作る
        asset_type = AccountType.query.filter_by(code="asset").first()
        db.session.add(Account(
            user_id=other.id, code="9999", name="他人の現金",
            account_type_id=asset_type.id,
        ))
        db.session.add(FiscalClose(
            user_id=other.id, year=2026, closed_period=5,
        ))
        je = JournalEntry(
            user_id=other.id,
            entry_number=1,
        )
        db.session.add(je)
        db.session.flush()
        db.session.add(JournalEntryLine(
            journal_entry_id=je.id,
            account_user_id=other.id, account_code="9999",
            debit_amount=999, credit_amount=0,
        ))
        db.session.add(MedicalExpense(
            user_id=other.id,
        ))
        db.session.add(BalanceCacheBlob(
            user_id=other.id, year=2026, period=5,
            encrypted_blob=b"\xff\xff", blob_iv=b"\x00" * 12,
        ))
        db.session.commit()

        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        # 他人の account
        assert all(a["code"] != "9999" for a in data["accounts"])
        # 他人の fiscal_close
        assert all(
            fc["year"] != 2026 or fc["closed_period"] != 5
            for fc in data["fiscal_closes"]
        )
        # 他人の journal_entries / lines / medical は一切含まれない (auth user は
        # 自分の仕訳・医療費を持たないので各リストは空)。E3-F PR-D-6-4 で平文
        # description/amount_paid は export されなくなったため空リストで検証する。
        assert data["journal_entries"] == []
        assert data["journal_entry_lines"] == []
        assert data["medical_expenses"] == []
        # 他人の BCB (year/period が同じ組み合わせでも他人のは含まれない)
        # ※ 自分の BCB が存在しなければ list 自体空
        assert data["balance_cache_blobs"] == []

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


class TestBackupExportVouchers:
    """v5 BU-2a: Voucher (画像本体含む) のエクスポート。"""

    def _make_voucher(self, db, user_id, key="vouchers/x.jpg", journal_id=None):
        from app.models.voucher import Voucher
        v = Voucher(
            user_id=user_id,
            journal_entry_id=journal_id,
            image_key=key,
            file_hash="a" * 64, file_size=100,
        )
        db.session.add(v)
        db.session.flush()
        return v

    def test_voucher_data_included_with_image_b64(
        self, app, client, db, user, auth_header, monkeypatch,
    ):
        import base64
        from app.services import storage as storage_module
        from app.views import api as api_module

        class FakeStorage:
            def get(self, k):
                return b"\xff\xd8\xff\xe0FAKE-JPEG"

        backend = FakeStorage()
        monkeypatch.setattr(storage_module, "get_storage_backend", lambda: backend)
        monkeypatch.setattr(api_module, "get_storage_backend", lambda: backend)

        self._make_voucher(db, user.id)
        db.session.commit()

        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        vouchers = resp.get_json()["data"]["vouchers"]
        assert len(vouchers) == 1
        v = vouchers[0]
        assert v["image_key"] == "vouchers/x.jpg"
        assert v["image_data"] == base64.b64encode(b"\xff\xd8\xff\xe0FAKE-JPEG").decode()
        assert "_imageError" not in v

    def test_voucher_storage_failure_localized(
        self, app, client, db, user, auth_header, monkeypatch,
    ):
        """ストレージ I/O 失敗で全体 500 にならず、当該行に _imageError。"""
        from app.services import storage as storage_module
        from app.views import api as api_module

        class FlakyStorage:
            def get(self, k):
                if "boom" in k:
                    raise IOError("disk gone")
                return b"OK"

        backend = FlakyStorage()
        monkeypatch.setattr(storage_module, "get_storage_backend", lambda: backend)
        monkeypatch.setattr(api_module, "get_storage_backend", lambda: backend)

        self._make_voucher(db, user.id, key="vouchers/ok.jpg")
        self._make_voucher(db, user.id, key="vouchers/boom.jpg")
        db.session.commit()

        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        vouchers = resp.get_json()["data"]["vouchers"]
        ok = [v for v in vouchers if v["image_key"] == "vouchers/ok.jpg"]
        bad = [v for v in vouchers if v["image_key"] == "vouchers/boom.jpg"]
        assert len(ok) == 1
        assert ok[0]["image_data"] is not None
        assert "_imageError" not in ok[0]
        assert len(bad) == 1
        assert bad[0]["image_data"] is None
        assert "disk gone" in bad[0]["_imageError"]

    def test_voucher_other_user_excluded(
        self, app, client, db, user, auth_header, monkeypatch,
    ):
        from app.models.user import User
        from app.services import storage as storage_module
        from app.views import api as api_module
        backend = type("S", (), {"get": lambda self, k: b"x"})()
        monkeypatch.setattr(storage_module, "get_storage_backend", lambda: backend)
        monkeypatch.setattr(api_module, "get_storage_backend", lambda: backend)

        other = User(
            username="other_v", email="other_v@example.com", user_type="personal",
        )
        other.set_password("password123")
        db.session.add(other)
        db.session.flush()
        self._make_voucher(db, other.id, key="vouchers/other.jpg")
        db.session.commit()

        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        keys = [v["image_key"] for v in resp.get_json()["data"]["vouchers"]]
        assert "vouchers/other.jpg" not in keys

    def test_voucher_soft_deleted_excluded(
        self, app, client, db, user, auth_header, monkeypatch,
    ):
        """論理削除済 (deleted_at != None) は export 対象外。"""
        from datetime import datetime, timezone
        from app.services import storage as storage_module
        from app.views import api as api_module
        backend = type("S", (), {"get": lambda self, k: b"x"})()
        monkeypatch.setattr(storage_module, "get_storage_backend", lambda: backend)
        monkeypatch.setattr(api_module, "get_storage_backend", lambda: backend)

        v = self._make_voucher(db, user.id, key="vouchers/gone.jpg")
        v.deleted_at = datetime.now(timezone.utc)
        db.session.commit()

        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        keys = [v["image_key"] for v in resp.get_json()["data"]["vouchers"]]
        assert "vouchers/gone.jpg" not in keys


class TestBackupExportRemainingTables:
    """v5 BU-2b: AIDraft / UserAIConfig / WebhookConfig / TaxFormMapping /
    CsvColumnProfile のエクスポート。"""

    def _stub_storage(self, monkeypatch, payload=b"\x01\x02"):
        from app.services import storage as storage_module
        from app.views import api as api_module
        backend = type("S", (), {"get": lambda self, k: payload})()
        monkeypatch.setattr(storage_module, "get_storage_backend", lambda: backend)
        monkeypatch.setattr(api_module, "get_storage_backend", lambda: backend)

    def test_ai_draft_included_with_image(
        self, app, client, db, user, auth_header, monkeypatch,
    ):
        import base64
        from app.models.ai_draft import AIDraft
        self._stub_storage(monkeypatch, b"\xff\xd8FAKE")
        db.session.add(AIDraft(
            user_id=user.id, image_key="drafts/p.jpg", image_mime="image/jpeg",
            status="pending", comment="メモ",
        ))
        db.session.commit()
        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        drafts = resp.get_json()["data"]["ai_drafts"]
        assert len(drafts) == 1
        assert drafts[0]["image_key"] == "drafts/p.jpg"
        assert drafts[0]["status"] == "pending"
        assert drafts[0]["image_data"] == base64.b64encode(b"\xff\xd8FAKE").decode()

    def test_ai_config_included_with_blob(
        self, app, client, db, user, auth_header,
    ):
        import base64
        from app.models.ai_config import UserAIConfig
        db.session.add(UserAIConfig(
            user_id=user.id, provider="openai", model_name="gpt-4",
            api_key_blob=b"\xaa\xbb", api_key_iv=b"\x00" * 12,
        ))
        db.session.commit()
        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        cfg = resp.get_json()["data"]["user_ai_config"]
        assert cfg["provider"] == "openai"
        assert cfg["model_name"] == "gpt-4"
        assert cfg["api_key_blob"] == base64.b64encode(b"\xaa\xbb").decode()

    def test_ai_config_null_when_unset(
        self, app, client, db, user, auth_header,
    ):
        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        assert resp.get_json()["data"]["user_ai_config"] is None

    def test_webhook_csv_tax_included(
        self, app, client, db, user, accounts, auth_header,
    ):
        from app.models.webhook import WebhookConfig
        from app.models.csv_column_profile import CsvColumnProfile
        from app.models.tax_form import TaxFormField, TaxFormMapping
        db.session.add(WebhookConfig(
            user_id=user.id, name="Discord", provider="discord",
            webhook_url="https://example.com/wh",
            events_json='["import_success"]',
        ))
        db.session.add(CsvColumnProfile(
            user_id=user.id, account_code="1010",
            date_col=0, desc_col=1, deposit_col=2, withdrawal_col=3,
            date_format="%Y/%m/%d",
        ))
        f = TaxFormField(
            form_type="general", page=1, section="revenue",
            row_code="1", name="売上",
            account_type_code="revenue", display_order=1,
        )
        db.session.add(f)
        db.session.flush()
        db.session.add(TaxFormMapping(
            user_id=user.id, account_code="1010", field_id=f.id,
        ))
        db.session.commit()
        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert len(data["webhook_configs"]) == 1
        assert data["webhook_configs"][0]["webhook_url"] == "https://example.com/wh"
        assert len(data["csv_column_profiles"]) == 1
        assert data["csv_column_profiles"][0]["account_code"] == "1010"
        assert len(data["tax_form_mappings"]) == 1
        assert data["tax_form_mappings"][0]["account_code"] == "1010"

    def test_remaining_tables_isolated_per_user(
        self, app, client, db, user, accounts, auth_header,
    ):
        """他人の WebhookConfig / CsvColumnProfile / TaxFormMapping /
        UserAIConfig / AIDraft が含まれないこと。"""
        from app.models.account import Account, AccountType
        from app.models.ai_config import UserAIConfig
        from app.models.ai_draft import AIDraft
        from app.models.csv_column_profile import CsvColumnProfile
        from app.models.tax_form import TaxFormField, TaxFormMapping
        from app.models.user import User
        from app.models.webhook import WebhookConfig
        other = User(
            username="other_b", email="other_b@example.com", user_type="personal",
        )
        other.set_password("password123")
        db.session.add(other)
        db.session.flush()
        # 他人の科目 (TaxFormMapping FK の都合)
        asset_type = AccountType.query.filter_by(code="asset").first()
        db.session.add(Account(
            user_id=other.id, code="8888", name="他人科目",
            account_type_id=asset_type.id,
        ))
        db.session.flush()
        db.session.add(UserAIConfig(
            user_id=other.id, provider="anthropic", model_name="claude-x",
        ))
        db.session.add(WebhookConfig(
            user_id=other.id, name="OtherWh", provider="discord",
            webhook_url="https://leak.example/x",
            events_json='["import_success"]',
        ))
        db.session.add(CsvColumnProfile(
            user_id=other.id, account_code="8888",
            date_col=0, desc_col=1, amount_col=2,
            amount_mode="single",
        ))
        db.session.add(AIDraft(
            user_id=other.id, image_key="drafts/other.jpg",
            image_mime="image/jpeg", status="pending",
        ))
        f = TaxFormField(
            form_type="general", page=1, section="revenue",
            row_code="9", name="他人欄",
            account_type_code="asset", display_order=99,
        )
        db.session.add(f)
        db.session.flush()
        db.session.add(TaxFormMapping(
            user_id=other.id, account_code="8888", field_id=f.id,
        ))
        db.session.commit()

        resp = client.get("/api/v1/backup/export", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        # AI 設定は本人のみ (= None or 本人のもの)
        if data["user_ai_config"] is not None:
            assert data["user_ai_config"]["model_name"] != "claude-x"
        # Webhook / CSV / TaxMapping / AIDraft の他人エントリが混じらない
        assert all(
            w["webhook_url"] != "https://leak.example/x"
            for w in data["webhook_configs"]
        )
        assert all(p["account_code"] != "8888" for p in data["csv_column_profiles"])
        assert all(t["account_code"] != "8888" for t in data["tax_form_mappings"])
        assert all(
            d["image_key"] != "drafts/other.jpg" for d in data["ai_drafts"]
        )


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


class TestAuditProxyWriteBlock:
    """Lv3 監査人が代理閲覧 (acting_as_user_id セッション) 中に
    `encrypted_blob` 付き batch POST または restore を叩くと 403 を返す
    回帰テスト (PR #251 review M-1)。

    クライアント側 AAD は監査人 ID で構築されるため、owner DB に保存すると
    AAD 不一致で永続的に復号不能になる。平文 POST は既存設計 (Lv3=本人同等)
    で許可されているのでブロックしない。
    """

    def _set_acting_as(self, db, client, owner, auditor, level=3):
        from app.models.audit import AuditGrant
        grant = AuditGrant(
            owner_user_id=owner.id,
            auditor_user_id=auditor.id,
            permission_level=level,
            status="active",
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = owner.id
            sess["acting_as_permission_level"] = level

    def test_batch_with_encrypted_blob_blocked_in_proxy(
            self, client, db, user, auditor, accounts,
    ):
        # entry レベルに encrypted_blob があれば 403
        self._set_acting_as(db, client, user, auditor, level=3)
        resp = client.post("/api/v1/journals/batch", json={
            "entries": [
                {
                    "date": "2026-02-01", "description": "テスト",
                    "encrypted_blob": "AAAA", "blob_iv": "BBBB",
                    "lines": [
                        {"account_code": accounts["5010"].code, "debit": 100},
                        {"account_code": accounts["1010"].code, "credit": 100},
                    ],
                },
            ],
        })
        assert resp.status_code == 403
        assert "代理閲覧" in resp.get_json()["error"]

    def test_batch_with_line_blob_blocked_in_proxy(
            self, client, db, user, auditor, accounts,
    ):
        # line レベルに encrypted_blob があっても 403 (lines を再帰チェック)
        self._set_acting_as(db, client, user, auditor, level=3)
        resp = client.post("/api/v1/journals/batch", json={
            "entries": [
                {
                    "date": "2026-02-01", "description": "テスト",
                    "lines": [
                        {"account_code": accounts["5010"].code, "debit": 100,
                         "encrypted_blob": "AAAA", "blob_iv": "BBBB"},
                        {"account_code": accounts["1010"].code, "credit": 100},
                    ],
                },
            ],
        })
        assert resp.status_code == 403

    def test_batch_plaintext_rejected_in_proxy(
            self, client, db, user, auditor, accounts,
    ):
        # PR-C 以降 平文-only POST は 400 で拒否される。代理閲覧 Lv3 でも
        # ガード対象外 (= proxy_block を通過) だが、encrypted_blob 必須化に
        # より別経路で 400 になる。
        self._set_acting_as(db, client, user, auditor, level=3)
        resp = client.post("/api/v1/journals/batch", json={
            "entries": [
                {
                    "date": "2026-02-01", "description": "テスト",
                    "lines": [
                        {"account_code": accounts["5010"].code, "debit": 100},
                        {"account_code": accounts["1010"].code, "credit": 100},
                    ],
                },
            ],
        })
        assert resp.status_code == 400
        assert "encrypted_blob" in resp.get_json()["error"]

    def test_restore_blocked_in_proxy(self, client, db, user, auditor):
        # restore は破壊的全置換のため、暗号化有無に関わらず 403
        self._set_acting_as(db, client, user, auditor, level=3)
        resp = client.post("/api/v1/backup/restore", json={
            "version": "1.0", "user_id": user.id, "data": {},
        })
        assert resp.status_code == 403
        assert "代理閲覧" in resp.get_json()["error"]


class TestUpdateJournal:
    """PUT /api/v1/journals/<id> の動作確認。

    cashbook / journal の編集経路がクライアント側暗号化に移行する際の
    共通エンドポイント。フィールドと lines を全置換する。
    """

    def test_success_with_encrypted_blob(self, client, db, user, accounts, auth_header):
        """PUT も PR-C 以降 entry + 全 line に encrypted_blob 必須。"""
        entry = make_journal(
            db, user.id, accounts["5010"].code, accounts["1010"].code, 500,
        )
        import base64
        resp = client.put(f"/api/v1/journals/{entry.id}",
                          headers=auth_header,
                          json={
                              "fiscal_year": 2026, "fiscal_month": 3,
                              "encrypted_blob": base64.b64encode(b"\x00" * 48).decode(),
                              "blob_iv": base64.b64encode(b"\x00" * 12).decode(),
                              "lines": [
                                  {"account_code": accounts["5010"].code, "debit": 500,
                                   "encrypted_blob": base64.b64encode(b"\x11" * 48).decode(),
                                   "blob_iv": base64.b64encode(b"\x11" * 12).decode()},
                                  {"account_code": accounts["1010"].code, "credit": 500,
                                   "encrypted_blob": base64.b64encode(b"\x22" * 48).decode(),
                                   "blob_iv": base64.b64encode(b"\x22" * 12).decode()},
                              ],
                          })
        assert resp.status_code == 200, resp.get_json()
        from app.extensions import db as _db
        _db.session.refresh(entry)
        assert entry.encrypted_blob is not None
        assert entry.blob_iv is not None
        for line in entry.lines:
            assert line.encrypted_blob is not None
            assert line.blob_iv is not None

    def test_missing_body_returns_400(self, client, db, user, accounts, auth_header):
        entry = make_journal(
            db, user.id, accounts["5010"].code, accounts["1010"].code, 500,
        )
        # JSON ボディなし (Content-Type のみ) → 400
        resp = client.put(f"/api/v1/journals/{entry.id}", headers=auth_header)
        assert resp.status_code == 400
        assert "JSON" in resp.get_json()["error"]

    def test_not_found(self, client, db, user, accounts, auth_header):
        resp = client.put("/api/v1/journals/99999",
                          headers=auth_header,
                          json={
                              "date": "2026-03-15",
                              "description": "x",
                              "lines": [
                                  {"account_code": accounts["5010"].code, "debit": 100},
                                  {"account_code": accounts["1010"].code, "credit": 100},
                              ],
                          })
        assert resp.status_code == 404

    def test_unbalanced_returns_400(self, client, db, user, accounts, auth_header):
        entry = make_journal(
            db, user.id, accounts["5010"].code, accounts["1010"].code, 1000,
        )
        resp = client.put(f"/api/v1/journals/{entry.id}",
                          headers=auth_header,
                          json={
                              "fiscal_year": 2026, "fiscal_month": 3,
                              "lines": encrypt_lines([
                                  {"account_code": accounts["5010"].code, "debit": 200},
                                  {"account_code": accounts["1010"].code, "credit": 100},
                              ]),
                              **encrypted_payload(),
                          })
        assert resp.status_code == 400
        assert "一致" in resp.get_json()["error"]

    def test_locked_period_returns_400(self, client, db, user, accounts, auth_header):
        from app.models.fiscal import FiscalClose
        entry = make_journal(
            db, user.id, accounts["5010"].code, accounts["1010"].code, 100,
            entry_date=date(2026, 5, 15),
        )
        # 5 月までを確定済みにする → PUT 不可
        fc = FiscalClose(user_id=user.id, year=2026, closed_period=5)
        db.session.add(fc)
        db.session.commit()
        resp = client.put(f"/api/v1/journals/{entry.id}",
                          headers=auth_header,
                          json={
                              "date": "2026-05-20", "description": "x",
                              "lines": encrypt_lines([
                                  {"account_code": accounts["5010"].code, "debit": 100},
                                  {"account_code": accounts["1010"].code, "credit": 100},
                              ]),
                              **encrypted_payload(),
                          })
        assert resp.status_code == 400

    def _setup_proxy(self, db, client, owner, auditor):
        from app.models.audit import AuditGrant
        g = AuditGrant(owner_user_id=owner.id, auditor_user_id=auditor.id,
                       permission_level=3, status="active")
        db.session.add(g)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = owner.id
            sess["acting_as_permission_level"] = 3

    def test_proxy_encrypted_update_blocked(self, client, db, user, auditor, accounts):
        entry = make_journal(
            db, user.id, accounts["5010"].code, accounts["1010"].code, 100,
        )
        self._setup_proxy(db, client, user, auditor)
        import base64
        resp = client.put(f"/api/v1/journals/{entry.id}", json={
            "date": "2026-03-15", "description": "x",
            "encrypted_blob": base64.b64encode(b"\x00" * 48).decode(),
            "blob_iv": base64.b64encode(b"\x00" * 12).decode(),
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
        })
        assert resp.status_code == 403
        assert "代理閲覧" in resp.get_json()["error"]

    def test_proxy_line_only_encrypted_blocked(
            self, client, db, user, auditor, accounts,
    ):
        # entry-level 暗号化なし、line-level 暗号化ありのリクエストが
        # 代理閲覧中に通ってしまわないこと。
        entry = make_journal(
            db, user.id, accounts["5010"].code, accounts["1010"].code, 100,
        )
        self._setup_proxy(db, client, user, auditor)
        import base64
        resp = client.put(f"/api/v1/journals/{entry.id}", json={
            "date": "2026-03-15", "description": "x",
            "lines": [
                {"account_code": accounts["5010"].code, "debit": 100,
                 "encrypted_blob": base64.b64encode(b"\x11" * 48).decode(),
                 "blob_iv": base64.b64encode(b"\x11" * 12).decode()},
                {"account_code": accounts["1010"].code, "credit": 100},
            ],
        })
        assert resp.status_code == 403
        assert "代理閲覧" in resp.get_json()["error"]

    def test_new_date_in_locked_period_returns_400(
            self, client, db, user, accounts, auth_header,
    ):
        # 既存 entry は未確定、PUT の新 fiscal_month が確定済み月 → 400
        # (check_period_open_for_new 分岐のカバー)
        from app.models.fiscal import FiscalClose
        entry = make_journal(
            db, user.id, accounts["5010"].code, accounts["1010"].code, 100,
            entry_date=date(2026, 10, 15),  # 未確定月
        )
        fc = FiscalClose(user_id=user.id, year=2026, closed_period=5)
        db.session.add(fc)
        db.session.commit()
        resp = client.put(f"/api/v1/journals/{entry.id}",
                          headers=auth_header,
                          json={
                              "fiscal_year": 2026, "fiscal_month": 3,  # 確定済み月
                              "lines": encrypt_lines([
                                  {"account_code": accounts["5010"].code, "debit": 100},
                                  {"account_code": accounts["1010"].code, "credit": 100},
                              ]),
                              **encrypted_payload(),
                          })
        assert resp.status_code == 400
        assert "確定済み" in resp.get_json()["error"]

    def test_fiscal_month_16_rejected(self, client, db, user, accounts, auth_header):
        """損益振替期間 (fiscal_month=16) は手動入力禁止なので PUT でも 400。"""
        entry = make_journal(
            db, user.id, accounts["5010"].code, accounts["1010"].code, 100,
        )
        resp = client.put(f"/api/v1/journals/{entry.id}",
                          headers=auth_header,
                          json={
                              "fiscal_year": 2026, "fiscal_month": 16,
                              "lines": encrypt_lines([
                                  {"account_code": accounts["5010"].code, "debit": 100},
                                  {"account_code": accounts["1010"].code, "credit": 100},
                              ]),
                              **encrypted_payload(),
                          })
        assert resp.status_code == 400
        assert "損益振替" in resp.get_json()["error"]
