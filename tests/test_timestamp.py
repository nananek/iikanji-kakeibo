"""入力期限チェック（Phase 4: 電帳法タイムスタンプ）テスト"""

from datetime import date, datetime, timezone, timedelta

import pytest

from app.views.helpers import check_deadline, DEADLINE_DAYS
from app.models.voucher import Voucher
from tests.conftest import make_journal, make_voucher


class TestCheckDeadline:
    """check_deadline ヘルパーのテスト"""

    def test_within_deadline(self):
        receipt = date(2026, 1, 1)
        uploaded = date(2026, 2, 15)  # 45 days
        assert check_deadline(receipt, uploaded) is False

    def test_exceeded_deadline(self):
        receipt = date(2026, 1, 1)
        uploaded = date(2026, 3, 20)  # 79 days
        assert check_deadline(receipt, uploaded) is True

    def test_exactly_67_days(self):
        receipt = date(2026, 1, 1)
        uploaded = receipt + timedelta(days=67)
        assert check_deadline(receipt, uploaded) is False

    def test_68_days_exceeded(self):
        receipt = date(2026, 1, 1)
        uploaded = receipt + timedelta(days=68)
        assert check_deadline(receipt, uploaded) is True

    def test_none_receipt_date(self):
        assert check_deadline(None, date(2026, 1, 1)) is False

    def test_none_uploaded_date(self):
        assert check_deadline(date(2026, 1, 1), None) is False

    def test_datetime_uploaded(self):
        """uploaded_date が datetime でも正しく動作"""
        receipt = date(2026, 1, 1)
        uploaded = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)
        assert check_deadline(receipt, uploaded) is True


class TestVoucherListDeadline:
    """証憑一覧 (E3-F PR-D-4-4 でクライアント描画) の期限超過バッジ。

    期限超過 (uploaded - 仕訳日 > 67日) の判定・バッジ描画は仕訳日 (平文 date)
    に依存するため、サーバではなくクライアント (index_renderer.mjs の
    buildVoucherCards) が復号済み仕訳日から算出する。サーバ HTML には
    バッジが出ないことを担保 (ロジック検証は test_voucher_index_cards.mjs)。
    E3-F PR-D-6-3: Bearer API の deadline_exceeded フィールドは date 依存の
    ため撤去した (旧 TestAPIDeadline は削除)。
    """

    def test_badge_not_server_rendered(self, db, logged_in_client, user, accounts):
        old_date = date(2025, 10, 1)
        entry = make_journal(
            db, user.id, "5010", "1010", 1000,
            source="ai_receipt", entry_date=old_date,
        )
        v = Voucher(
            user_id=user.id,
            journal_entry_id=entry.id,
            image_key="vouchers/1/1.jpg",
            image_mime="image/jpeg",
            uploaded_at=datetime(2026, 1, 15, tzinfo=timezone.utc),  # 106 days
        )
        db.session.add(v)
        db.session.commit()

        resp = logged_in_client.get("/vouchers/")
        html = resp.data.decode()
        # バッジはクライアント描画 → サーバ HTML には出ない。
        assert "bi-clock-history" not in html
        # クライアントが期限判定するための非暗号化メタ (uploaded_at) は渡る。
        assert "uploaded_at" in html


# E3-F PR-D-6-3: 旧 TestAPIDeadline (Bearer 証憑 API の deadline_exceeded
# フィールド検証) は削除した。期限超過判定は date (D-6-5 で DROP) に依存する
# ため API 応答から撤去し、ブラウザ証憑一覧 (復号済み仕訳日から算出) が担う。
