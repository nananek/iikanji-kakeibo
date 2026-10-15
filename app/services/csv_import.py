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
    """金額文字列をパースして整数を返す（符号保持）

    対応形式: "1,234", "¥1,234", "￥1,234", "-500", "1234円", 空文字→0
    マイナス値はそのまま返す（呼び出し側で反転処理を行う）
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
        return int(float(s))
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
        }
        date_format_str: strftime形式の日付フォーマット

    Returns:
        list of {
            "row_num": int,
            "date": date or None,
            "description": str,
            "deposit": int,     # 入金額（>=0）
            "withdrawal": int,  # 出金額（>=0）
            "raw_row": list[str],
        }

    Notes:
        マイナス値は自動的に反転して逆側に振り分ける。
        例: 出金列に -500 → 入金 500 として扱う（クレカのキャッシュバック等）
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

        if deposit_col is not None:
            deposit = parse_amount(safe_get(deposit_col))
        if withdrawal_col is not None:
            withdrawal = parse_amount(safe_get(withdrawal_col))

        # マイナス値は反転して逆側に振り分ける
        # 例: 出金列に -500（クレカのキャッシュバック）→ 入金 500
        if deposit < 0:
            withdrawal += abs(deposit)
            deposit = 0
        if withdrawal < 0:
            deposit += abs(withdrawal)
            withdrawal = 0

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


# --- 列マッピングの推定・正規化 ---

# 日本の銀行・クレカ CSV でよく使われる列名のキーワード。
# 完全一致ではなく「ヘッダーに部分文字列として含まれるか」で判定する。
_DATE_KEYWORDS = (
    "利用日", "取引日", "ご利用日", "計上日", "処理日", "日付", "年月日", "日時",
)
_DESC_KEYWORDS = (
    "利用店", "ご利用店", "利用先", "店舗", "摘要", "内容", "備考", "説明",
    "相手先", "相手", "取引内容", "ご利用内容",
)
_DEPOSIT_KEYWORDS = (
    "入金", "預入", "お預り", "お預かり", "返金", "振込入金", "収入",
)
_WITHDRAWAL_KEYWORDS = (
    "出金", "お支払", "支払金額", "支払額", "利用金額", "ご利用金額", "利用額",
    "引出", "払出", "キャッシング",
)
_AMOUNT_KEYWORDS = ("金額", "取引金額", "決済額")


def _match_header(headers, keywords):
    """ヘッダー配列から keywords のいずれかを含む列インデックスを返す。

    先に現れたキーワードを優先する (利用日 > 日付 など)。見つからなければ None。
    """
    lowered = [str(h).strip() for h in headers]
    for kw in keywords:
        for idx, h in enumerate(lowered):
            if kw in h:
                return idx
    return None


def guess_date_format(values):
    """サンプル値から date_format (strftime) を推定する。既定は %Y/%m/%d。"""
    for value in values:
        if not value or not str(value).strip():
            continue
        s = str(value).strip()
        for _, fmt in DATE_FORMATS:
            try:
                datetime.strptime(s, fmt)
                return fmt
            except ValueError:
                continue
    return "%Y/%m/%d"


def detect_mapping_heuristic(headers):
    """ヘッダー名から列マッピングを推定する (AI を使わない簡易版)。

    日付列・摘要列が特定できない場合は None を返す。入金/出金が 1 列に
    まとまっている場合は withdrawal として扱う (クレカ明細で一般的)。
    """
    date_col = _match_header(headers, _DATE_KEYWORDS)
    desc_col = _match_header(headers, _DESC_KEYWORDS)
    if date_col is None or desc_col is None:
        return None

    deposit_col = _match_header(headers, _DEPOSIT_KEYWORDS)
    withdrawal_col = _match_header(headers, _WITHDRAWAL_KEYWORDS)
    if deposit_col is None and withdrawal_col is None:
        amount_col = _match_header(headers, _AMOUNT_KEYWORDS)
        if amount_col is not None:
            withdrawal_col = amount_col

    return {
        "date_col": date_col,
        "desc_col": desc_col,
        "deposit_col": deposit_col,
        "withdrawal_col": withdrawal_col,
        "date_format": "%Y/%m/%d",
    }


def normalize_mapping(mapping, num_cols):
    """外部 (MCP/API) から渡されたマッピングを検証・正規化する。

    列インデックスが範囲内の整数であることを確認し、範囲外・必須欠落は
    :class:`ValueError` を送出する。成功時は date_format を補完した dict を返す。
    """
    if not isinstance(mapping, dict):
        raise ValueError("mapping はオブジェクトで指定してください。")

    result = {}
    for key in ("date_col", "desc_col", "deposit_col", "withdrawal_col"):
        value = mapping.get(key)
        if value is None:
            result[key] = None
            continue
        try:
            value = int(value)
        except (ValueError, TypeError):
            raise ValueError(f"mapping.{key} は整数で指定してください。")
        if not (0 <= value < num_cols):
            raise ValueError(f"mapping.{key} が列数の範囲外です。")
        result[key] = value

    if result["date_col"] is None or result["desc_col"] is None:
        raise ValueError("mapping.date_col と mapping.desc_col は必須です。")

    result["date_format"] = mapping.get("date_format") or "%Y/%m/%d"
    return result


def resolve_mapping(user_id, payment_account_code, headers, sample_rows,
                    explicit_mapping=None, explicit_date_format=None):
    """明示指定 → 保存プロファイル → ヒューリスティック の順で列マッピングを解決する。

    Returns:
        (mapping, source) または (None, None)。
        source は ``"explicit"`` / ``"saved"`` / ``"heuristic"``。
    """
    num_cols = len(headers)
    if num_cols == 0:
        return None, None

    if explicit_mapping:
        mapping = normalize_mapping(explicit_mapping, num_cols)
        if explicit_date_format:
            mapping["date_format"] = explicit_date_format
        elif not explicit_mapping.get("date_format"):
            dc = mapping["date_col"]
            mapping["date_format"] = guess_date_format(
                [(r[dc] if dc < len(r) else "") for r in sample_rows]
            )
        return mapping, "explicit"

    profile = load_column_profile(user_id, payment_account_code)
    if profile:
        cols_valid = all(
            profile.get(k) is None or (
                isinstance(profile.get(k), int)
                and 0 <= profile[k] < num_cols
            )
            for k in ("date_col", "desc_col", "deposit_col",
                      "withdrawal_col", "amount_col")
        )
        if cols_valid and profile.get("date_col") is not None \
                and profile.get("desc_col") is not None:
            return profile, "saved"

    heuristic = detect_mapping_heuristic(headers)
    if heuristic:
        date_values = []
        if 0 <= heuristic["date_col"] < num_cols:
            date_values = [
                (r[heuristic["date_col"]] if heuristic["date_col"] < len(r) else "")
                for r in sample_rows
            ]
        heuristic["date_format"] = guess_date_format(date_values)
        return heuristic, "heuristic"

    return None, None


def serialize_parsed_rows(parsed):
    """parse_csv_full() の結果を JSON 化可能な形に変換する。"""
    result = []
    for p in parsed:
        d = p.get("date")
        result.append({
            "row_num": p.get("row_num"),
            "date": d.isoformat() if hasattr(d, "isoformat") else d,
            "description": p.get("description", ""),
            "deposit": int(p.get("deposit") or 0),
            "withdrawal": int(p.get("withdrawal") or 0),
        })
    return result


# --- 列プロファイル管理 ---

logger = logging.getLogger(__name__)


def save_column_profile(user_id, account_code, mapping_data, date_format):
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
        "amount_col": None,
        "date_format": date_format,
        "amount_mode": "separate",
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
  "deposit_col": 入金（預入・収入）列のインデックス（0始まり、なければ null）,
  "withdrawal_col": 出金（引出・支払）列のインデックス（0始まり、なければ null）,
  "date_format": 日付のstrftime形式（例: "%Y/%m/%d", "%Y-%m-%d"）
}}

注意:
- インデックスは0始まりの整数です
- 日本の銀行・クレカCSVでよくあるパターンを考慮してください
- 入金と出金が別々の列にある場合: それぞれの列を指定
- 金額が1つの列にまとまっている場合: 口座の種類に応じて判断
  - 銀行口座: 入金=預入、出金=引出
  - クレジットカード: 利用額=出金（withdrawal_col）、キャッシュバック等=入金（deposit_col）
  - 入出金が1列なら、その列を withdrawal_col に指定し deposit_col は null
- 日付形式は実際のデータに合わせてください"""


def detect_columns_by_ai(user_id, headers, sample_rows):
    """AIを使ってCSVの列マッピングを推定する

    Returns:
        dict or None: マッピング結果。失敗時は None。
    """
    from app.models.ai_config import UserAIConfig
    from app.services.ai_receipt import (
        _get_ai_config, _TEXT_PROVIDER_HANDLERS, _extract_json, _call_ai_text,
    )

    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    if not config:
        return None

    try:
        api_key, provider, model, _, __, extra_kw = _get_ai_config(
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
        result = _call_ai_text(
            text_handler, api_key, model, prompt, 500, user_id, extra_kw,
            provider=provider, feature="csv_columns_detect",
        )
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

        date_format = result.get("date_format", "%Y/%m/%d")

        mapping = {
            "date_col": date_col,
            "desc_col": desc_col,
            "date_format": date_format,
            "deposit_col": None,
            "withdrawal_col": None,
        }

        deposit_col = result.get("deposit_col")
        withdrawal_col = result.get("withdrawal_col")
        if deposit_col is not None:
            deposit_col = int(deposit_col)
            if 0 <= deposit_col < num_cols:
                mapping["deposit_col"] = deposit_col
        if withdrawal_col is not None:
            withdrawal_col = int(withdrawal_col)
            if 0 <= withdrawal_col < num_cols:
                mapping["withdrawal_col"] = withdrawal_col

        return mapping
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "AI column detection returned invalid data for user %s",
            user_id, exc_info=True,
        )
        return None
