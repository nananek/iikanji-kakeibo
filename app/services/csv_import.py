"""CSV明細取り込みサービス"""

import csv
import io
import logging
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


# --- 列プロファイル管理 ---

logger = logging.getLogger(__name__)


def save_column_profile(user_id, account_code, mapping_data, date_format,
                        amount_mode):
    """CSV列マッピングプロファイルを保存/更新する"""
    from app.extensions import db
    from app.models.csv_column_profile import CsvColumnProfile

    profile = CsvColumnProfile.query.filter_by(
        user_id=user_id, account_code=account_code
    ).first()

    fields = {
        "date_col": mapping_data["date_col"],
        "desc_col": mapping_data["desc_col"],
        "deposit_col": mapping_data.get("deposit_col"),
        "withdrawal_col": mapping_data.get("withdrawal_col"),
        "amount_col": mapping_data.get("amount_col"),
        "date_format": date_format,
        "amount_mode": amount_mode,
    }

    if profile:
        for k, v in fields.items():
            setattr(profile, k, v)
    else:
        profile = CsvColumnProfile(
            user_id=user_id, account_code=account_code, **fields
        )
        db.session.add(profile)
    db.session.commit()
    return profile


def load_column_profile(user_id, account_code):
    """保存済みCSV列マッピングプロファイルを読み込む

    Returns:
        dict or None
    """
    from app.models.csv_column_profile import CsvColumnProfile

    profile = CsvColumnProfile.query.filter_by(
        user_id=user_id, account_code=account_code
    ).first()
    if profile:
        return profile.to_mapping_dict()
    return None


# --- AI列自動検出 ---

CSV_COLUMN_DETECT_PROMPT = """あなたは日本の家計簿アプリのアシスタントです。
以下はCSVファイルのヘッダーとサンプルデータです。列マッピングを推定してください。

ヘッダー:
{headers_text}

サンプルデータ（先頭{sample_count}行）:
{sample_text}

以下のJSON形式のみを返してください。余計なテキストは不要です。

{{
  "date_col": 日付列のインデックス（0始まり）,
  "desc_col": 摘要・説明列のインデックス（0始まり）,
  "amount_mode": "separate" または "single",
  "deposit_col": 入金列のインデックス（amount_mode が "separate" の場合、なければ null）,
  "withdrawal_col": 出金列のインデックス（amount_mode が "separate" の場合、なければ null）,
  "amount_col": 金額列のインデックス（amount_mode が "single" の場合、なければ null）,
  "date_format": 日付のstrftime形式（例: "%Y/%m/%d", "%Y-%m-%d"）
}}

注意:
- インデックスは0始まりの整数です
- 日本の銀行・クレカCSVでよくあるパターンを考慮してください
- 入金・出金が別列の場合は amount_mode を "separate" に
- 正負の符号で入出金を区別する1列の場合は amount_mode を "single" に
- 日付形式は実際のデータに合わせてください"""


def detect_columns_by_ai(user_id, headers, sample_rows):
    """AIを使ってCSVの列マッピングを推定する

    Returns:
        dict or None: マッピング結果。失敗時は None。
    """
    from app.models.ai_config import UserAIConfig
    from app.services.ai_receipt import (
        _get_ai_config, _TEXT_PROVIDER_HANDLERS, _extract_json,
    )

    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    if not config:
        return None

    try:
        api_key, provider, model, _, __, extra_kw, ___ = _get_ai_config(
            user_id
        )
    except (ValueError, RuntimeError):
        return None

    text_handler = _TEXT_PROVIDER_HANDLERS.get(provider)
    if not text_handler:
        return None

    headers_text = ", ".join(f"[{i}] {h}" for i, h in enumerate(headers))
    sample_lines = []
    for row in sample_rows[:5]:
        sample_lines.append(", ".join(row))
    sample_text = "\n".join(sample_lines)

    prompt = CSV_COLUMN_DETECT_PROMPT.format(
        headers_text=headers_text,
        sample_count=len(sample_lines),
        sample_text=sample_text,
    )

    try:
        text_kw = {"max_tokens": 500, **extra_kw}
        result = text_handler(api_key, model, prompt, **text_kw)
    except Exception:
        logger.warning(
            "AI column detection failed for user %s", user_id, exc_info=True,
        )
        return None

    # バリデーション
    num_cols = len(headers)
    try:
        date_col = int(result["date_col"])
        desc_col = int(result["desc_col"])
        if not (0 <= date_col < num_cols) or not (0 <= desc_col < num_cols):
            return None

        amount_mode = result.get("amount_mode", "separate")
        date_format = result.get("date_format", "%Y/%m/%d")

        mapping = {
            "date_col": date_col,
            "desc_col": desc_col,
            "amount_mode": amount_mode,
            "date_format": date_format,
        }

        if amount_mode == "single":
            amount_col = result.get("amount_col")
            if amount_col is not None:
                amount_col = int(amount_col)
                if 0 <= amount_col < num_cols:
                    mapping["amount_col"] = amount_col
            mapping.setdefault("amount_col", None)
            mapping["deposit_col"] = None
            mapping["withdrawal_col"] = None
        else:
            deposit_col = result.get("deposit_col")
            withdrawal_col = result.get("withdrawal_col")
            mapping["deposit_col"] = None
            mapping["withdrawal_col"] = None
            if deposit_col is not None:
                deposit_col = int(deposit_col)
                if 0 <= deposit_col < num_cols:
                    mapping["deposit_col"] = deposit_col
            if withdrawal_col is not None:
                withdrawal_col = int(withdrawal_col)
                if 0 <= withdrawal_col < num_cols:
                    mapping["withdrawal_col"] = withdrawal_col
            mapping["amount_col"] = None

        return mapping
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "AI column detection returned invalid data for user %s",
            user_id, exc_info=True,
        )
        return None
