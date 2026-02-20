"""csv_import サービスのテスト"""

from datetime import date

import pytest

from app.services.csv_import import (
    detect_encoding,
    parse_amount,
    parse_csv_full,
    parse_csv_preview,
    parse_date,
)


# ============================================================
# detect_encoding
# ============================================================

class TestDetectEncoding:
    def test_utf8(self):
        # utf-8-sig は BOM なし UTF-8 も受け入れるので先にマッチする
        enc = detect_encoding("日本語".encode("utf-8"))
        assert enc in ("utf-8-sig", "utf-8")

    def test_utf8_bom(self):
        assert detect_encoding("日本語".encode("utf-8-sig")) == "utf-8-sig"

    def test_cp932(self):
        raw = "日本語テスト".encode("cp932")
        assert detect_encoding(raw) in ("cp932", "shift_jis")

    def test_shift_jis(self):
        # Shift-JIS 固有文字（CP932 と同じだが明示的にテスト）
        raw = "あいうえお".encode("shift_jis")
        enc = detect_encoding(raw)
        assert raw.decode(enc) == "あいうえお"

    def test_euc_jp(self):
        # EUC-JP 固有のバイト列 — 短い文字列は UTF-8/CP932 で先にデコード
        # されることがあるため、十分に長い文字列で検証
        text = "日本語のエンコーディング判定をテストする文章です。表示確認用。"
        raw = text.encode("euc-jp")
        enc = detect_encoding(raw)
        assert raw.decode(enc) == text

    def test_ascii(self):
        assert detect_encoding(b"hello,world") == "utf-8-sig"

    def test_empty(self):
        enc = detect_encoding(b"")
        assert enc == "utf-8-sig"


# ============================================================
# parse_amount
# ============================================================

class TestParseAmount:
    def test_plain_integer(self):
        assert parse_amount("1234") == 1234

    def test_comma_separated(self):
        assert parse_amount("1,234,567") == 1234567

    def test_yen_sign_fullwidth(self):
        assert parse_amount("￥1,234") == 1234

    def test_yen_sign_halfwidth(self):
        assert parse_amount("¥5,000") == 5000

    def test_yen_sign_unicode(self):
        assert parse_amount("\u00a51,000") == 1000

    def test_yen_suffix(self):
        assert parse_amount("1234円") == 1234

    def test_negative_returns_absolute(self):
        assert parse_amount("-500") == 500

    def test_float_truncated(self):
        assert parse_amount("1234.56") == 1234

    def test_empty_string(self):
        assert parse_amount("") == 0

    def test_none(self):
        assert parse_amount(None) == 0

    def test_whitespace_only(self):
        assert parse_amount("   ") == 0

    def test_dash_only(self):
        assert parse_amount("-") == 0

    def test_non_numeric(self):
        assert parse_amount("abc") == 0

    def test_padded_whitespace(self):
        assert parse_amount("  1,000  ") == 1000


# ============================================================
# parse_date
# ============================================================

class TestParseDate:
    def test_yyyy_slash(self):
        assert parse_date("2026/01/15", "%Y/%m/%d") == date(2026, 1, 15)

    def test_yyyy_hyphen(self):
        assert parse_date("2026-01-15", "%Y-%m-%d") == date(2026, 1, 15)

    def test_japanese_format(self):
        assert parse_date("2026年1月15日", "%Y年%m月%d日") == date(2026, 1, 15)

    def test_yy_slash(self):
        assert parse_date("26/01/15", "%y/%m/%d") == date(2026, 1, 15)

    def test_mm_dd_yyyy(self):
        assert parse_date("01/15/2026", "%m/%d/%Y") == date(2026, 1, 15)

    def test_fallback_auto_detect(self):
        """指定フォーマットが合わなくてもフォールバックで解析"""
        result = parse_date("2026/03/20", "%Y-%m-%d")  # 不一致フォーマット
        assert result == date(2026, 3, 20)

    def test_empty_string(self):
        assert parse_date("", "%Y/%m/%d") is None

    def test_none(self):
        assert parse_date(None, "%Y/%m/%d") is None

    def test_whitespace_only(self):
        assert parse_date("   ", "%Y/%m/%d") is None

    def test_invalid_date(self):
        assert parse_date("not-a-date", "%Y/%m/%d") is None

    def test_whitespace_trimmed(self):
        assert parse_date("  2026/01/15  ", "%Y/%m/%d") == date(2026, 1, 15)


# ============================================================
# parse_csv_preview
# ============================================================

class TestParseCsvPreview:
    def _make_csv(self, text, encoding="utf-8"):
        return text.encode(encoding)

    def test_basic(self):
        raw = self._make_csv("日付,摘要,入金,出金\n2026/01/01,給料,300000,\n2026/01/05,食費,,5000\n")
        result = parse_csv_preview(raw)
        assert result["headers"] == ["日付", "摘要", "入金", "出金"]
        assert result["total_rows"] == 2
        assert len(result["rows"]) == 2
        assert result["rows"][0] == ["2026/01/01", "給料", "300000", ""]

    def test_max_rows(self):
        lines = ["日付,金額"] + [f"2026/01/{i:02d},{i*100}" for i in range(1, 31)]
        raw = self._make_csv("\n".join(lines))
        result = parse_csv_preview(raw, max_rows=5)
        assert len(result["rows"]) == 5
        assert result["total_rows"] == 30

    def test_empty_csv(self):
        result = parse_csv_preview(b"")
        assert result["headers"] == []
        assert result["rows"] == []
        assert result["total_rows"] == 0

    def test_header_only(self):
        raw = self._make_csv("日付,摘要,金額\n")
        result = parse_csv_preview(raw)
        assert result["headers"] == ["日付", "摘要", "金額"]
        assert result["rows"] == []
        assert result["total_rows"] == 0

    def test_blank_rows_skipped(self):
        raw = self._make_csv("H1,H2\n\n  ,  \nA,B\n")
        result = parse_csv_preview(raw)
        assert result["total_rows"] == 1
        assert result["rows"][0] == ["A", "B"]

    def test_utf8_bom(self):
        raw = "\ufeff日付,金額\n2026/01/01,100\n".encode("utf-8-sig")
        result = parse_csv_preview(raw)
        assert result["headers"] == ["日付", "金額"]

    def test_cp932_encoding(self):
        raw = "日付,金額\n2026/01/01,100\n".encode("cp932")
        result = parse_csv_preview(raw)
        assert result["encoding"] in ("cp932", "shift_jis")
        assert result["headers"] == ["日付", "金額"]
        assert result["total_rows"] == 1


# ============================================================
# parse_csv_full
# ============================================================

class TestParseCsvFull:
    def _make_csv(self, text, encoding="utf-8"):
        return text.encode(encoding)

    def _two_col_mapping(self):
        return {
            "date_col": 0,
            "desc_col": 1,
            "deposit_col": 2,
            "withdrawal_col": 3,
        }

    def _single_col_mapping(self):
        return {
            "date_col": 0,
            "desc_col": 1,
            "amount_col": 2,
        }

    def test_two_column_deposit_withdrawal(self):
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "2026/01/01,給料,300000,\n"
            "2026/01/05,食費,,5000\n"
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 2
        assert rows[0]["date"] == date(2026, 1, 1)
        assert rows[0]["description"] == "給料"
        assert rows[0]["deposit"] == 300000
        assert rows[0]["withdrawal"] == 0
        assert rows[1]["deposit"] == 0
        assert rows[1]["withdrawal"] == 5000

    def test_single_column_amount(self):
        raw = self._make_csv(
            "日付,摘要,金額\n"
            "2026/01/01,給料,300000\n"
            "2026/01/05,食費,-5000\n"
        )
        rows = parse_csv_full(raw, self._single_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 2
        assert rows[0]["deposit"] == 300000
        assert rows[0]["withdrawal"] == 0
        assert rows[1]["deposit"] == 0
        assert rows[1]["withdrawal"] == 5000

    def test_row_num_is_1_indexed_with_header(self):
        raw = self._make_csv("日付,摘要,入金,出金\n2026/01/01,テスト,100,\n")
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert rows[0]["row_num"] == 2  # header=1, data starts at 2

    def test_raw_row_preserved(self):
        raw = self._make_csv("日付,摘要,入金,出金\n2026/01/01,テスト,100,\n")
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert rows[0]["raw_row"] == ["2026/01/01", "テスト", "100", ""]

    def test_blank_rows_skipped(self):
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "2026/01/01,A,100,\n"
            "  ,  ,  ,  \n"
            "2026/01/02,B,,200\n"
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 2

    def test_row_with_no_date_no_amount_skipped(self):
        """日付なし・金額0の行はスキップされる"""
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "invalid,メモのみ,,\n"
            "2026/01/01,有効,100,\n"
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 1
        assert rows[0]["description"] == "有効"

    def test_row_with_no_date_but_has_amount(self):
        """日付が無効でも金額があれば含まれる"""
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "invalid,メモ,1000,\n"
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 1
        assert rows[0]["date"] is None
        assert rows[0]["deposit"] == 1000

    def test_header_only_returns_empty(self):
        raw = self._make_csv("日付,摘要,金額\n")
        rows = parse_csv_full(raw, self._single_col_mapping(), "%Y/%m/%d")
        assert rows == []

    def test_single_row_returns_empty(self):
        """ヘッダーのみ（データ行なし）"""
        raw = self._make_csv("日付,摘要,金額")
        rows = parse_csv_full(raw, self._single_col_mapping(), "%Y/%m/%d")
        assert rows == []

    def test_column_index_out_of_range(self):
        """列インデックスが範囲外の場合は空文字扱い"""
        mapping = {
            "date_col": 0,
            "desc_col": 10,  # 存在しない列
            "deposit_col": 1,
            "withdrawal_col": None,
        }
        raw = self._make_csv("日付,金額\n2026/01/01,500\n")
        rows = parse_csv_full(raw, mapping, "%Y/%m/%d")
        assert len(rows) == 1
        assert rows[0]["description"] == ""
        assert rows[0]["deposit"] == 500

    def test_comma_in_amount(self):
        """金額にカンマが含まれる場合（single column）"""
        raw = self._make_csv(
            '日付,摘要,金額\n'
            '2026/01/01,テスト,"1,234,567"\n'
        )
        rows = parse_csv_full(raw, self._single_col_mapping(), "%Y/%m/%d")
        assert rows[0]["deposit"] == 1234567

    def test_cp932_csv(self):
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "2026/01/01,コンビニ,,500\n",
            encoding="cp932",
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 1
        assert rows[0]["description"] == "コンビニ"

    def test_yen_sign_in_amount(self):
        raw = self._make_csv(
            "日付,摘要,金額\n"
            "2026/01/01,テスト,¥1000\n"
        )
        rows = parse_csv_full(raw, self._single_col_mapping(), "%Y/%m/%d")
        assert rows[0]["deposit"] == 1000

    def test_multiple_rows_sequential_row_nums(self):
        lines = ["日付,摘要,入金,出金"] + [
            f"2026/01/{i:02d},item{i},{i*100}," for i in range(1, 6)
        ]
        raw = self._make_csv("\n".join(lines))
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 5
        assert [r["row_num"] for r in rows] == [2, 3, 4, 5, 6]
