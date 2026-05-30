"""証憑検索（Phase 2: 電帳法検索要件）テスト"""

from datetime import date

import pytest

from app.extensions import db
from app.models.voucher import Voucher
from tests.conftest import make_journal, make_voucher


class TestVoucherListPage:
    """証憑一覧画面 (E3-F PR-D-4-4 でクライアント描画に移行) のテスト。

    サーバは平文 (仕訳の date/description/金額) を読まず、証憑メタ JSON
    (id/journal_entry_id/entry_number/fiscal_year/uploaded_at/has_hash) を
    渡すだけ。実際のカード描画・電帳法 検索 (日付/金額/摘要) は
    index_renderer.mjs が MK 復号して行う (検証は test_voucher_index_cards.mjs)。
    """

    def test_renders_shell(self, db, logged_in_client, user):
        resp = logged_in_client.get("/vouchers/")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "vouchers-index-params" in body
        assert "vouchers-index-meta" in body
        assert "index_renderer.mjs" in body

    def test_empty_meta_when_no_vouchers(self, db, logged_in_client, user):
        resp = logged_in_client.get("/vouchers/")
        body = resp.data.decode()
        # 証憑なし → meta は空配列
        assert "vouchers-index-meta" in body
        assert "[]" in body

    def test_meta_includes_voucher(self, db, logged_in_client, user, accounts):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000, source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=entry.id)
        resp = logged_in_client.get("/vouchers/")
        body = resp.data.decode()
        # 非暗号化メタ (journal_entry_id / uploaded_at / has_hash) が含まれる
        assert "journal_entry_id" in body
        assert "uploaded_at" in body
        assert "has_hash" in body

    def test_orphan_voucher_in_meta(self, db, logged_in_client, user):
        """孤立証憑（journal_entry_id=NULL）も meta に含まれる"""
        v = Voucher(
            user_id=user.id,
            journal_entry_id=None,
            image_key="vouchers/1/orphan.jpg",
            image_mime="image/jpeg",
        )
        db.session.add(v)
        db.session.commit()
        resp = logged_in_client.get("/vouchers/")
        body = resp.data.decode()
        # journal_entry_id が null の証憑メタが含まれる (未紐付けバッジは client 描画)
        assert "null" in body
        assert "vouchers-index-meta" in body

    def test_does_not_render_plaintext(self, db, logged_in_client, user, accounts):
        # サーバは仕訳の平文 description/金額を読まない → HTML に出ない
        entry = make_journal(
            db, user.id, "5010", "1010", 13579,
            source="ai_receipt", description="ZZVOUCHERSECRET",
        )
        make_voucher(db, user.id, journal_entry_id=entry.id)
        resp = logged_in_client.get("/vouchers/")
        body = resp.data.decode()
        assert "ZZVOUCHERSECRET" not in body
        assert "13,579" not in body

    def test_user_isolation(self, app, db, user, accounts, second_user):
        """他ユーザーの証憑は meta に含まれない (空配列)"""
        entry = make_journal(
            db, user.id, "5010", "1010", 1000, source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=entry.id)

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(second_user.id)
        resp = client.get("/vouchers/")
        assert resp.status_code == 200
        body = resp.data.decode()
        # second_user の証憑は無い → meta は空配列
        assert '<script id="vouchers-index-meta" type="application/json">\n[]' in body


class TestAPIVoucherList:
    """API 証憑一覧のテスト"""

    def test_api_list_vouchers(self, client, db, user, accounts, auth_header):
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=entry.id)

        resp = client.get("/api/v1/vouchers", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["total"] == 1
        assert len(data["vouchers"]) == 1
        v = data["vouchers"][0]
        assert v["journal"]["amount"] == 1000
        assert v["journal"]["description"] is not None

    def test_api_list_empty(self, client, db, user, auth_header):
        resp = client.get("/api/v1/vouchers", headers=auth_header)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 0
        assert data["vouchers"] == []

    def test_api_date_filter(self, client, db, user, accounts, auth_header):
        e1 = make_journal(
            db, user.id, "5010", "1010", 500,
            source="ai_receipt", entry_date=date(2025, 1, 15),
        )
        make_voucher(db, user.id, journal_entry_id=e1.id)
        e2 = make_journal(
            db, user.id, "5010", "1010", 800,
            source="ai_receipt", entry_date=date(2025, 3, 20),
        )
        make_voucher(db, user.id, journal_entry_id=e2.id)

        resp = client.get(
            "/api/v1/vouchers?date_from=2025-03-01",
            headers=auth_header,
        )
        data = resp.get_json()
        assert data["total"] == 1
        assert data["vouchers"][0]["journal"]["amount"] == 800

    def test_api_search_filter(self, client, db, user, accounts, auth_header):
        e1 = make_journal(
            db, user.id, "5010", "1010", 500,
            source="ai_receipt", description="タクシー代",
        )
        make_voucher(db, user.id, journal_entry_id=e1.id)

        resp = client.get(
            "/api/v1/vouchers?search=タクシー",
            headers=auth_header,
        )
        data = resp.get_json()
        assert data["total"] == 1

    def test_api_orphan_voucher(self, client, db, user, auth_header):
        v = Voucher(
            user_id=user.id,
            journal_entry_id=None,
            image_key="vouchers/1/orphan.jpg",
            image_mime="image/jpeg",
        )
        db.session.add(v)
        db.session.commit()

        resp = client.get("/api/v1/vouchers", headers=auth_header)
        data = resp.get_json()
        assert data["total"] == 1
        assert data["vouchers"][0]["journal"] is None
