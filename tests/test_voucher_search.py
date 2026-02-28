"""証憑検索（Phase 2: 電帳法検索要件）テスト"""

from datetime import date

import pytest

from app.extensions import db
from app.models.voucher import Voucher
from tests.conftest import make_journal, make_voucher


class TestVoucherListPage:
    """証憑一覧画面のテスト"""

    def test_empty_list(self, db, logged_in_client, user):
        resp = logged_in_client.get("/vouchers/")
        assert resp.status_code == 200
        assert "証憑が見つかりません" in resp.data.decode()

    def test_lists_vouchers(self, db, logged_in_client, user, accounts):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
            source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=entry.id)
        resp = logged_in_client.get("/vouchers/")
        assert resp.status_code == 200
        assert "1,000" in resp.data.decode()

    def test_orphan_voucher_shown(self, db, logged_in_client, user):
        """孤立証憑（journal_entry_id=NULL）も表示される"""
        v = Voucher(
            user_id=user.id,
            journal_entry_id=None,
            image_key="vouchers/1/orphan.jpg",
            image_mime="image/jpeg",
        )
        db.session.add(v)
        db.session.commit()
        resp = logged_in_client.get("/vouchers/")
        assert resp.status_code == 200
        assert "未紐付け" in resp.data.decode()

    def test_user_isolation(self, app, db, user, accounts, second_user):
        """他ユーザーの証憑は表示されない"""
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
            source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=entry.id)

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(second_user.id)
        resp = client.get("/vouchers/")
        assert resp.status_code == 200
        assert "証憑が見つかりません" in resp.data.decode()


class TestVoucherDateFilter:
    """日付フィルタのテスト"""

    def _create_voucher_with_date(self, db, user, accounts, entry_date):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
            source="ai_receipt", entry_date=entry_date,
        )
        return make_voucher(db, user.id, journal_entry_id=entry.id)

    def test_date_from_filter(self, db, logged_in_client, user, accounts):
        self._create_voucher_with_date(db, user, accounts, date(2025, 1, 10))
        self._create_voucher_with_date(db, user, accounts, date(2025, 2, 20))

        resp = logged_in_client.get("/vouchers/?date_from=2025-02-01")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "2025/02/20" in html
        assert "2025/01/10" not in html

    def test_date_to_filter(self, db, logged_in_client, user, accounts):
        self._create_voucher_with_date(db, user, accounts, date(2025, 1, 10))
        self._create_voucher_with_date(db, user, accounts, date(2025, 2, 20))

        resp = logged_in_client.get("/vouchers/?date_to=2025-01-31")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "2025/01/10" in html
        assert "2025/02/20" not in html


class TestVoucherAmountFilter:
    """金額フィルタのテスト"""

    def test_amount_from_filter(self, db, logged_in_client, user, accounts):
        e1 = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
            source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=e1.id)
        e2 = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 5000,
            source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=e2.id)

        resp = logged_in_client.get("/vouchers/?amount_from=1000")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "5,000" in html
        # 500 の仕訳は含まれない
        assert html.count("card shadow-sm") == 1

    def test_amount_to_filter(self, db, logged_in_client, user, accounts):
        e1 = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
            source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=e1.id)
        e2 = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 5000,
            source="ai_receipt",
        )
        make_voucher(db, user.id, journal_entry_id=e2.id)

        resp = logged_in_client.get("/vouchers/?amount_to=1000")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert html.count("card shadow-sm") == 1


class TestVoucherSearchFilter:
    """摘要検索のテスト"""

    def test_search_description(self, db, logged_in_client, user, accounts):
        e1 = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
            source="ai_receipt", description="コンビニ購入",
        )
        make_voucher(db, user.id, journal_entry_id=e1.id)
        e2 = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 2000,
            source="ai_receipt", description="ランチ代",
        )
        make_voucher(db, user.id, journal_entry_id=e2.id)

        resp = logged_in_client.get("/vouchers/?search=コンビニ")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "コンビニ購入" in html
        assert "ランチ代" not in html


class TestAPIVoucherList:
    """API 証憑一覧のテスト"""

    def test_api_list_vouchers(self, client, db, user, accounts, auth_header):
        entry = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 1000,
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
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
            source="ai_receipt", entry_date=date(2025, 1, 15),
        )
        make_voucher(db, user.id, journal_entry_id=e1.id)
        e2 = make_journal(
            db, user.id, accounts["5010"].id, accounts["1010"].id, 800,
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
            db, user.id, accounts["5010"].id, accounts["1010"].id, 500,
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
