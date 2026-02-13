"""CSV明細取り込みサービス"""

import csv
import io
import re
from datetime import date, datetime


# 日本の銀行・クレカCSVで使われる主要な日付フォーマット
DATE_FORMATS = [
    ("YYYY/MM/DD", "%Y/%m/%d"),
    ("YYYY-MM-DD", "%Y-%m-%d"),
    ("YYYY年MM月DD日", "%Y年%m月%d日"),
    ("YY/MM/DD", "%y/%m/%d"),
    ("MM/DD/YYYY", "%m/%d/%Y"),
]


def detect_encoding(raw_bytes):
    """Shift-JIS / UTF-8 / CP932 を自動判定"""
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc-jp"):
        try:
            raw_bytes.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def parse_csv_preview(raw_bytes, max_rows=20):
    """CSVバイト列を読み込み、ヘッダーとプレビュー行を返す

    Returns:
        {
            "encoding": str,
            "headers": list[str],   # 列ヘッダー（なければ Col0, Col1, ...）
            "rows": list[list[str]], # プレビュー行（max_rows件）
            "total_rows": int,
        }
    """
    encoding = detect_encoding(raw_bytes)
    text = raw_bytes.decode(encoding)

    # BOM除去
    if text.startswith("\ufeff"):
        text = text[1:]

    reader = csv.reader(io.StringIO(text))
    all_rows = [row for row in reader if any(cell.strip() for cell in row)]

    if not all_rows:
        return {"encoding": encoding, "headers": [], "rows": [], "total_rows": 0}

    headers = all_rows[0]
    data_rows = all_rows[1:]

    return {
        "encoding": encoding,
        "headers": headers,
        "rows": data_rows[:max_rows],
        "total_rows": len(data_rows),
    }


def parse_amount(value):
    """金額文字列をパースして整数を返す

    対応形式: "1,234", "¥1,234", "￥1,234", "-500", "1234円", 空文字→0
    """
    if not value or not value.strip():
        return 0
    s = value.strip()
    s = s.replace(",", "").replace("¥", "").replace("￥", "").replace("円", "")
    s = s.replace("\u00a5", "")  # 半角円記号
    s = s.strip()
    if not s or s == "-":
        return 0
    try:
        return abs(int(float(s)))
    except ValueError:
        return 0


def parse_date(value, date_format_str):
    """日付文字列をパースしてdateオブジェクトを返す"""
    if not value or not value.strip():
        return None
    s = value.strip()
    try:
        return datetime.strptime(s, date_format_str).date()
    except ValueError:
        pass
    # フォールバック: 全フォーマットを試す
    for _, fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_csv_full(raw_bytes, mapping, date_format_str):
    """CSVをフルパースして取り込み用データを返す

    Args:
        mapping: {
            "date_col": int,
            "desc_col": int,
            "deposit_col": int or None,   # 入金列
            "withdrawal_col": int or None, # 出金列
            "amount_col": int or None,     # 入出金が1列の場合
        }
        date_format_str: strftime形式の日付フォーマット

    Returns:
        list of {
            "row_num": int,
            "date": date or None,
            "description": str,
            "deposit": int,     # 入金額
            "withdrawal": int,  # 出金額
            "raw_row": list[str],
        }
    """
    encoding = detect_encoding(raw_bytes)
    text = raw_bytes.decode(encoding)
    if text.startswith("\ufeff"):
        text = text[1:]

    reader = csv.reader(io.StringIO(text))
    all_rows = [row for row in reader if any(cell.strip() for cell in row)]

    if len(all_rows) < 2:
        return []

    data_rows = all_rows[1:]  # ヘッダー行をスキップ
    results = []

    date_col = mapping["date_col"]
    desc_col = mapping["desc_col"]
    deposit_col = mapping.get("deposit_col")
    withdrawal_col = mapping.get("withdrawal_col")
    amount_col = mapping.get("amount_col")

    for i, row in enumerate(data_rows):
        if not any(cell.strip() for cell in row):
            continue

        def safe_get(idx):
            if idx is not None and 0 <= idx < len(row):
                return row[idx].strip()
            return ""

        parsed_date = parse_date(safe_get(date_col), date_format_str)
        description = safe_get(desc_col)

        deposit = 0
        withdrawal = 0

        if amount_col is not None:
            # 入出金が1列: 正=入金, 負=出金
            raw_val = safe_get(amount_col)
            cleaned = raw_val.replace(",", "").replace("¥", "").replace("￥", "").replace("円", "").strip()
            if cleaned and cleaned != "-":
                try:
                    num = int(float(cleaned))
                    if num >= 0:
                        deposit = num
                    else:
                        withdrawal = abs(num)
                except ValueError:
                    pass
        else:
            if deposit_col is not None:
                deposit = parse_amount(safe_get(deposit_col))
            if withdrawal_col is not None:
                withdrawal = parse_amount(safe_get(withdrawal_col))

        if parsed_date is None and deposit == 0 and withdrawal == 0:
            continue

        results.append({
            "row_num": i + 2,  # 1-indexed, header=1
            "date": parsed_date,
            "description": description,
            "deposit": deposit,
            "withdrawal": withdrawal,
            "raw_row": row,
        })

    return results
