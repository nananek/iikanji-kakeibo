"""ofx_import サービスのテスト"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.services.ofx_import import parse_ofx


def _make_txn(amount, payee="", memo="", txn_date=None):
    """テスト用 OFX トランザクションを作成"""
    txn = MagicMock()
    txn.amount = Decimal(str(amount))
    txn.payee = payee
    txn.memo = memo
    txn.date = txn_date
    return txn


def _make_ofx(transactions, account_id="1234567", account_type="checking"):
    """テスト用 OFX オブジェクトを作成してパッチする"""
    ofx = MagicMock()
    ofx.account.account_id = account_id
    ofx.account.account_type = account_type
    ofx.account.statement.transactions = transactions
    return ofx


class TestParseOfx:
    def test_deposit(self):
        txns = [_make_txn(50000, payee="給与振込", txn_date=datetime(2026, 1, 25))]
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        assert len(result["rows"]) == 1
        row = result["rows"][0]
        assert row["deposit"] == 50000
        assert row["withdrawal"] == 0
        assert row["date"] == "2026-01-25"
        assert row["description"] == "給与振込"

    def test_withdrawal(self):
        txns = [_make_txn(-3000, payee="コンビニ", txn_date=datetime(2026, 1, 5))]
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        row = result["rows"][0]
        assert row["deposit"] == 0
        assert row["withdrawal"] == 3000

    def test_zero_amount(self):
        txns = [_make_txn(0, payee="手数料無料", txn_date=datetime(2026, 1, 1))]
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        row = result["rows"][0]
        assert row["deposit"] == 0
        assert row["withdrawal"] == 0

    def test_account_info(self):
        txns = [_make_txn(100, payee="test", txn_date=datetime(2026, 1, 1))]
        with patch("app.services.ofx_import.OfxParser.parse",
                    return_value=_make_ofx(txns, account_id="9876543", account_type="savings")):
            result = parse_ofx(b"dummy")
        assert result["account_id"] == "9876543"
        assert result["account_type"] == "savings"

    def test_description_payee_only(self):
        txns = [_make_txn(100, payee="店名", memo="", txn_date=datetime(2026, 1, 1))]
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        assert result["rows"][0]["description"] == "店名"

    def test_description_memo_only(self):
        txns = [_make_txn(100, payee="", memo="メモのみ", txn_date=datetime(2026, 1, 1))]
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        assert result["rows"][0]["description"] == "メモのみ"

    def test_description_payee_and_memo(self):
        txns = [_make_txn(100, payee="店名", memo="カード払い", txn_date=datetime(2026, 1, 1))]
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        assert result["rows"][0]["description"] == "店名 カード払い"

    def test_description_payee_memo_duplicate(self):
        """payee と memo が同じ場合は重複除去"""
        txns = [_make_txn(100, payee="店名", memo="店名", txn_date=datetime(2026, 1, 1))]
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        assert result["rows"][0]["description"] == "店名"

    def test_description_none_values(self):
        txns = [_make_txn(100, txn_date=datetime(2026, 1, 1))]
        txns[0].payee = None
        txns[0].memo = None
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        assert result["rows"][0]["description"] == ""

    def test_no_date(self):
        txns = [_make_txn(100, payee="test", txn_date=None)]
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        assert result["rows"][0]["date"] is None

    def test_row_numbering(self):
        txns = [
            _make_txn(100, payee=f"txn{i}", txn_date=datetime(2026, 1, i))
            for i in range(1, 6)
        ]
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        assert [r["row_num"] for r in result["rows"]] == [1, 2, 3, 4, 5]

    def test_empty_transactions(self):
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx([])):
            result = parse_ofx(b"dummy")
        assert result["rows"] == []
        assert result["account_id"] == "1234567"

    def test_no_statement(self):
        """statement が None の場合"""
        ofx = MagicMock()
        ofx.account.account_id = "1234567"
        ofx.account.account_type = "checking"
        ofx.account.statement = None
        with patch("app.services.ofx_import.OfxParser.parse", return_value=ofx):
            result = parse_ofx(b"dummy")
        assert result["rows"] == []

    def test_multiple_transactions_mixed(self):
        txns = [
            _make_txn(300000, payee="給料", txn_date=datetime(2026, 1, 25)),
            _make_txn(-50000, payee="家賃", txn_date=datetime(2026, 1, 27)),
            _make_txn(-3000, payee="コンビニ", txn_date=datetime(2026, 1, 28)),
            _make_txn(1000, payee="利息", txn_date=datetime(2026, 1, 31)),
        ]
        with patch("app.services.ofx_import.OfxParser.parse", return_value=_make_ofx(txns)):
            result = parse_ofx(b"dummy")
        rows = result["rows"]
        assert len(rows) == 4
        assert rows[0]["deposit"] == 300000
        assert rows[1]["withdrawal"] == 50000
        assert rows[2]["withdrawal"] == 3000
        assert rows[3]["deposit"] == 1000

    def test_missing_account_attributes(self):
        """account_id / account_type が存在しない場合"""
        ofx = MagicMock()
        ofx.account.statement.transactions = []
        del ofx.account.account_id
        del ofx.account.account_type
        with patch("app.services.ofx_import.OfxParser.parse", return_value=ofx):
            result = parse_ofx(b"dummy")
        assert result["account_id"] == ""
        assert result["account_type"] == ""
