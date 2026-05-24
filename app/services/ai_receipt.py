"""AI証憑読取サービス - 証憑画像からの仕訳データ抽出"""

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

import httpx
from cryptography.fernet import Fernet
from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.ai_config import UserAIConfig
from app.models.journal import JournalEntry, JournalEntryLine

logger = logging.getLogger(__name__)


@dataclass
class DocumentAnalysis:
    """第1ラウンド: 書類解析結果"""

    date: str | None
    description: str
    amount: int
    document_type: str
    items: list[dict] = field(default_factory=list)
    needs_ledger: bool = False
    requested_accounts: list[str] = field(default_factory=list)


@dataclass
class JournalSuggestion:
    """仕訳案"""

    title: str
    description: str
    date: str | None
    entry_description: str
    lines: list[dict] = field(default_factory=list)
    compliance: dict | None = None


# --- 暗号化ヘルパー ---


def _get_fernet():
    """SECRET_KEY から Fernet インスタンスを生成"""
    secret = current_app.config["SECRET_KEY"]
    key_bytes = hashlib.sha256(secret.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt_api_key(plain_key: str) -> bytes:
    """APIキーを暗号化"""
    return _get_fernet().encrypt(plain_key.encode())


def decrypt_api_key(encrypted: bytes) -> str:
    """暗号化されたAPIキーを復号。SECRET_KEY 変更時は InvalidToken になる。"""
    try:
        return _get_fernet().decrypt(encrypted).decode()
    except Exception:
        raise ValueError(
            "APIキーの復号に失敗しました。SECRET_KEYが変更された可能性があります。"
            "設定画面でAPIキーを再登録してください。"
        )


# --- プロバイダー設定 ---

PROVIDER_DEFAULTS = {
    "openai": "gpt-4o",
    "google": "gemini-2.0-flash",
    "anthropic": "claude-sonnet-4-20250514",
    "llama_cpp": "default",
}

PROVIDER_LABELS = {
    "openai": "OpenAI (GPT-4o)",
    "google": "Google Gemini",
    "anthropic": "Anthropic Claude",
    "llama_cpp": "llama.cpp (サーバー提供)",
}


def is_llama_cpp_available(app_config=None) -> bool:
    """サーバー管理者が llama.cpp エンドポイントを設定しているか。"""
    if app_config is None:
        from flask import current_app
        app_config = current_app.config
    return bool((app_config.get("LLAMA_CPP_URL") or "").strip())


def get_available_provider_labels(app_config=None) -> dict:
    """UI で表示すべきプロバイダー候補を返す。

    llama.cpp はサーバー設定 (LLAMA_CPP_URL) があるときのみ含める。
    """
    labels = {k: v for k, v in PROVIDER_LABELS.items() if k != "llama_cpp"}
    if is_llama_cpp_available(app_config):
        labels["llama_cpp"] = PROVIDER_LABELS["llama_cpp"]
    return labels

RECEIPT_PROMPT = """あなたは日本の家計簿アプリのアシスタントです。
領収書・レシートの画像を解析して、以下のJSON形式で情報を抽出してください。

必ず以下の形式のJSONのみを返してください。余計なテキストは不要です。

{
  "date": "YYYY-MM-DD形式の日付。読み取れない場合はnull",
  "description": "店名や取引内容の摘要",
  "amount": 合計金額（税込、整数）,
  "category": "以下の費目から最も適切なものを1つ選択: 食費, 住居費, 水道光熱費, 通信費, 交通費, 日用品費, 被服費, 美容費, 交際費, 趣味・娯楽費, 教育費, 医療費, 雑費"
}

注意事項:
- 金額は合計（税込）を整数で返してください
- 日本円以外の通貨は無視してください
- 日付が読み取れない場合はnullとしてください
- 複数の商品があっても合計金額を1つ返してください"""


DOCUMENT_PROMPT = """あなたは日本の家計簿アプリのアシスタントです。
添付された画像（領収書、給与明細、請求書、その他の証憑）を解析して、以下のJSON形式で情報を抽出してください。

必ず以下の形式のJSONのみを返してください。余計なテキストは不要です。

{
  "date": "YYYY-MM-DD形式の日付。読み取れない場合はnull",
  "description": "取引の摘要（店名、取引内容、給与支給元等）",
  "amount": 主要な合計金額（整数）,
  "document_type": "receipt / payslip / invoice / other のいずれか",
  "items": [
    {"name": "項目名", "amount": 金額}
  ],
  "needs_ledger": true または false,
  "requested_accounts": ["参照したい勘定科目名"]
}

注意事項:
- 金額は整数で返してください
- document_type は書類の種類を判別してください
  - receipt: 領収書・レシート
  - payslip: 給与明細・給料明細
  - invoice: 請求書
  - other: その他
- items には明細行や内訳を含めてください
  - 給与明細の場合: 基本給、各種手当、社会保険料、所得税、住民税、控除額、差引支給額など
  - レシートの場合: 主要な商品名と金額（読み取れる範囲で）
  - 請求書の場合: 請求項目と金額
- needs_ledger: 正確な仕訳を提案するために過去の元帳データの参照が必要な場合は true
  - 給与明細のように複数科目に分かれる複雑な書類では true にしてください
  - 単純なレシートでは false で構いません
- requested_accounts: needs_ledger が true の場合、参照したい勘定科目名のリスト
  - 例: ["給料手当", "法定福利費", "預り金"]"""


WEB_IMPORT_PROMPT = """あなたは日本の家計簿アプリのアシスタントです。
以下はユーザーが銀行・クレジットカード・証券口座などのWebページからコピーしたテキストです。
このテキストから取引明細を読み取り、以下のJSON形式で返してください。

必ず以下の形式のJSONのみを返してください。余計なテキストは不要です。

{
  "transactions": [
    {
      "date": "YYYY-MM-DD形式の日付",
      "description": "取引内容・摘要",
      "deposit": 入金額（整数、なければ0）,
      "withdrawal": 出金額（整数、なければ0）
    }
  ]
}

注意事項:
- 金額は整数で返してください（カンマ・円記号は除去）
- 入金（収入・預入・振込入金等）は deposit に、出金（支出・引落・振込出金等）は withdrawal に入れてください
- 日付が読み取れない行はスキップせず、date を null にしてください
- ヘッダー行や合計行、残高行は含めないでください
- 明細の順序はテキストに現れる順序のまま返してください
- クレジットカード明細の場合、利用金額は withdrawal として扱ってください

取込先口座: __PAYMENT_ACCOUNT_NAME__

--- Webページのテキスト ---
__RAW_TEXT__"""


COMPLIANCE_CHECK_PROMPT = """

## 電帳法コンプライアンスチェック（必ず実施してください）
この画像が電子帳簿保存法（スキャナ保存）の要件を満たすか判定し、JSONに以下のフィールドを追加してください。

"compliance": {
  "status": "pass" または "warn" または "fail",
  "warnings": ["警告メッセージ1", "警告メッセージ2"],
  "details": ["チェック項目1の結果", "チェック項目2の結果", ...]
}

チェック項目:
1. **画像品質**: ピンぼけ、影かぶり、画像の切れ、歪みがないか
   - fail: 文字が読めないほどぼやけている、大きく切れている
   - warn: やや影がかかっている、端が少し切れている
2. **必須情報の視認性**: 日付、金額、取引先名が読み取れるか
   - fail: 日付・金額・取引先のいずれかが全く読み取れない
   - warn: 一部が不鮮明だが推測は可能
3. **書類の妥当性**: 領収書・請求書等の証憑として有効か
   - fail: 証憑ではない画像（風景写真等）
   - warn: メモ書き等で証憑としては不十分な可能性

status の判定:
- "pass": 全チェック項目に問題なし
- "warn": 軽微な問題あり（登録は可能だが撮り直し推奨）
- "fail": 重大な問題あり（証憑として不適格の可能性）

details には、status に関わらず全チェック項目の結果を簡潔に1行ずつ記載してください。
例: ["画像品質: 鮮明で読み取り可能", "必須情報: 日付・金額・取引先を確認", "書類妥当性: 領収書として有効"]

warnings が空の場合は status を "pass" にしてください。"""


CONSISTENCY_CHECK_PROMPT = """

## 仕訳整合性チェック（必ず実施してください）
この証憑画像から読み取れる情報を、以下の既存仕訳データと比較してください。

既存仕訳:
- 日付: {journal_date}
- 金額: {journal_amount}円
- 摘要: {journal_description}

JSONに以下のフィールドを追加してください:
"consistency": {{
  "status": "pass" または "warn" または "fail",
  "date_match": true/false,
  "amount_match": true/false,
  "description_match": true/false,
  "warnings": ["警告メッセージ1", ...]
}}

判定基準:
- date_match: 証憑の日付と仕訳日付が7日以内ならtrue
- amount_match: 証憑の金額と仕訳金額の差が10%以内ならtrue
- description_match: 証憑の店名/取引先と仕訳摘要に共通する語があればtrue
- status: 全matchならtrue→"pass"、1つでもfalse→"warn"、2つ以上false→"fail"
"""


# E2 PR-C-6a: クライアント完結 voucher attach 用のプレースホルダ版。
# 旧 CONSISTENCY_CHECK_PROMPT は Python str.format で {journal_date} 等を
# 置換する設計だったが、E2EE クライアント側 JS では __XXX__ 形式が安全
# (placeholder と JSON `{}` の混在を避ける)。
CONSISTENCY_CHECK_PROMPT_TEMPLATE = """

## 仕訳整合性チェック（必ず実施してください）
この証憑画像から読み取れる情報を、以下の既存仕訳データと比較してください。

既存仕訳:
- 日付: __JOURNAL_DATE__
- 金額: __JOURNAL_AMOUNT__円
- 摘要: __JOURNAL_DESCRIPTION__

JSONに以下のフィールドを追加してください:
"consistency": {
  "status": "pass" または "warn" または "fail",
  "date_match": true/false,
  "amount_match": true/false,
  "description_match": true/false,
  "warnings": ["警告メッセージ1", ...]
}

判定基準:
- date_match: 証憑の日付と仕訳日付が7日以内ならtrue
- amount_match: 証憑の金額と仕訳金額の差が10%以内ならtrue
- description_match: 証憑の店名/取引先と仕訳摘要に共通する語があればtrue
- status: 全matchならtrue→"pass"、1つでもfalse→"warn"、2つ以上false→"fail"
"""


def _build_suggestion_prompt(account_list_text, ledger_text="",
                             custom_prompt=""):
    """第2ラウンド用プロンプトを組み立てる"""
    ledger_section = ""
    if ledger_text:
        ledger_section = f"""
以下は関連する勘定科目の元帳データ（直近の取引）です：
{ledger_text}
"""

    custom_section = ""
    if custom_prompt:
        custom_section = f"""

## ユーザー定型ルール（必ず従ってください）
{custom_prompt}
"""

    return f"""あなたは日本の家計簿（複式簿記）アプリのアシスタントです。
先ほど解析した画像の内容を踏まえて、仕訳案を2〜4件提案してください。
各案はそれぞれ異なるアプローチや解釈を反映してください。

必ず以下の形式のJSONのみを返してください。余計なテキストは不要です。

{{
  "suggestions": [
    {{
      "title": "仕訳案の簡潔なタイトル（例: 食費として計上）",
      "description": "この仕訳案の説明や根拠",
      "date": "YYYY-MM-DD",
      "entry_description": "仕訳帳に記録する摘要",
      "lines": [
        {{"account_code": "勘定科目コード", "account_name": "勘定科目名", "debit_amount": 借方金額, "credit_amount": 0}},
        {{"account_code": "勘定科目コード", "account_name": "勘定科目名", "debit_amount": 0, "credit_amount": 貸方金額}}
      ]
    }}
  ]
}}
{ledger_section}
利用可能な勘定科目一覧（この中のコードと名前を使ってください）：
{account_list_text}
{custom_section}
注意事項:
- 各仕訳案の借方合計と貸方合計は必ず一致させてください
- account_code は上記一覧のコードを使ってください
- 金額は整数で返してください
- 給与明細等の複雑な書類では、詳細な内訳を反映した案と簡略化した案の両方を含めてください
- 異なる解釈（例: 費目の分類の違い、内訳の粒度の違い）を反映した複数案を提案してください"""


def _usage_openai_style(data: dict) -> dict:
    """OpenAI 互換 (OpenAI / llama.cpp) のレスポンスから usage を正規化。"""
    usage = data.get("usage") or {}
    return {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }


def _usage_anthropic_style(data: dict) -> dict:
    usage = data.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


def _usage_google_style(data: dict) -> dict:
    usage = data.get("usageMetadata") or {}
    return {
        "input_tokens": usage.get("promptTokenCount"),
        "output_tokens": usage.get("candidatesTokenCount"),
    }


def _extract_json(text: str) -> dict:
    """テキストからJSON部分を抽出してパース"""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise json.JSONDecodeError("JSONが見つかりません", text, 0)


# --- プロバイダー別API呼出し ---


def _call_openai(api_key: str, model: str, image_bytes: bytes,
                 mime_type: str, prompt: str = RECEIPT_PROMPT,
                 max_tokens: int = 500):
    """戻り値: (parsed_json, usage_dict)"""
    b64_image = base64.b64encode(image_bytes).decode()
    response = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{b64_image}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": max_tokens,
        },
        timeout=60.0,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _extract_json(content), _usage_openai_style(data)


def _call_google(api_key: str, model: str, image_bytes: bytes,
                 mime_type: str, prompt: str = RECEIPT_PROMPT,
                 max_tokens: int = 500):
    """戻り値: (parsed_json, usage_dict)"""
    b64_image = base64.b64encode(image_bytes).decode()
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64_image,
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": max_tokens,
            },
        },
        timeout=60.0,
    )
    response.raise_for_status()
    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text), _usage_google_style(data)


def _call_anthropic(api_key: str, model: str, image_bytes: bytes,
                    mime_type: str, prompt: str = RECEIPT_PROMPT,
                    max_tokens: int = 500):
    """戻り値: (parsed_json, usage_dict)"""
    b64_image = base64.b64encode(image_bytes).decode()
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": b64_image,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        },
        timeout=60.0,
    )
    response.raise_for_status()
    data = response.json()
    content = data["content"][0]["text"]
    return _extract_json(content), _usage_anthropic_style(data)


def _call_llama_cpp(api_key: str, model: str, image_bytes: bytes,
                    mime_type: str, prompt: str = RECEIPT_PROMPT,
                    max_tokens: int = 500, *, base_url: str = ""):
    """llama.cpp (llama-server) の OpenAI 互換エンドポイントを呼ぶ。

    デフォルトポートは 8080。マルチモーダル対応モデル (LLaVA など) が必要。
    戻り値: (parsed_json, usage_dict)
    """
    url = (base_url.rstrip("/") if base_url else "http://localhost:8080")
    b64_image = base64.b64encode(image_bytes).decode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = httpx.post(
        f"{url}/v1/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{b64_image}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": max_tokens,
        },
        timeout=120.0,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _extract_json(content), _usage_openai_style(data)


_PROVIDER_HANDLERS = {
    "openai": _call_openai,
    "google": _call_google,
    "anthropic": _call_anthropic,
    "llama_cpp": _call_llama_cpp,
}


# --- テキスト専用プロバイダー呼出し ---


def _call_openai_text(api_key: str, model: str, prompt: str,
                      max_tokens: int = 2000):
    """戻り値: (parsed_json, usage_dict)"""
    response = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=120.0,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _extract_json(content), _usage_openai_style(data)


def _call_google_text(api_key: str, model: str, prompt: str,
                      max_tokens: int = 2000):
    """戻り値: (parsed_json, usage_dict)"""
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": max_tokens,
            },
        },
        timeout=120.0,
    )
    response.raise_for_status()
    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text), _usage_google_style(data)


def _call_anthropic_text(api_key: str, model: str, prompt: str,
                         max_tokens: int = 2000):
    """戻り値: (parsed_json, usage_dict)"""
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120.0,
    )
    response.raise_for_status()
    data = response.json()
    content = data["content"][0]["text"]
    return _extract_json(content), _usage_anthropic_style(data)


def _call_llama_cpp_text(api_key: str, model: str, prompt: str,
                         max_tokens: int = 2000, *, base_url: str = ""):
    """llama.cpp (llama-server) のテキスト専用 OpenAI 互換呼び出し。

    戻り値: (parsed_json, usage_dict)
    """
    url = (base_url.rstrip("/") if base_url else "http://localhost:8080")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = httpx.post(
        f"{url}/v1/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=120.0,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _extract_json(content), _usage_openai_style(data)


_TEXT_PROVIDER_HANDLERS = {
    "openai": _call_openai_text,
    "google": _call_google_text,
    "anthropic": _call_anthropic_text,
    "llama_cpp": _call_llama_cpp_text,
}


# --- 元帳データ取得ヘルパー ---


def _get_ledger_context(user_id: int, account_names: list[str]) -> str:
    """指定された科目名に関連する元帳データをテキスト形式で返す"""
    accounts = Account.query.filter(
        Account.user_id == user_id,
        Account.is_active.is_(True),
    ).all()

    matched_accounts = []
    for acct in accounts:
        for name in account_names:
            if name in acct.name or acct.name in name:
                matched_accounts.append(acct)
                break

    if not matched_accounts:
        return ""

    lines = []
    for acct in matched_accounts:
        entries = (
            db.session.query(
                JournalEntry.date,
                JournalEntry.description,
                JournalEntryLine.debit_amount,
                JournalEntryLine.credit_amount,
            )
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .filter(
                db.and_(
                    JournalEntryLine.account_user_id == acct.user_id,
                    JournalEntryLine.account_code == acct.code,
                ),
            )
            .order_by(JournalEntry.date.desc())
            .limit(20)
            .all()
        )

        if entries:
            lines.append(f"\n【{acct.name}】（{acct.code}）")
            lines.append("日付 | 摘要 | 借方 | 貸方")
            lines.append("-" * 50)
            for e in entries:
                d = int(e.debit_amount)
                c = int(e.credit_amount)
                lines.append(
                    f"{e.date} | {e.description} | "
                    f"{'¥' + f'{d:,}' if d else '-'} | "
                    f"{'¥' + f'{c:,}' if c else '-'}"
                )

            # 残高集計
            totals = (
                db.session.query(
                    func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                    func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
                )
                .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
                .filter(
                    db.and_(
                        JournalEntryLine.account_user_id == acct.user_id,
                        JournalEntryLine.account_code == acct.code,
                    )
                )
                .first()
            )
            if totals:
                lines.append(
                    f"累計: 借方合計 ¥{int(totals[0]):,} / "
                    f"貸方合計 ¥{int(totals[1]):,}"
                )

    return "\n".join(lines)


def _get_account_list_text(user_id: int) -> str:
    """ユーザーの全勘定科目をテキスト一覧にする"""
    accounts = (
        Account.query
        .filter_by(user_id=user_id, is_active=True)
        .join(AccountType)
        .order_by(AccountType.display_order, Account.code)
        .all()
    )

    type_labels = {
        "asset": "資産",
        "liability": "負債",
        "equity": "純資産",
        "revenue": "収益",
        "expense": "費用",
    }

    lines = []
    current_type = None
    for acct in accounts:
        type_code = acct.account_type.code
        if type_code != current_type:
            current_type = type_code
            label = type_labels.get(type_code, type_code)
            lines.append(f"\n[{label}]")

        lines.append(f"  {acct.code} {acct.name}")

    return "\n".join(lines)


# --- メイン関数 ---


def _get_ai_config(user_id: int):
    """AI設定を取得してバリデーション

    Returns:
        (api_key, provider, model, handler, custom_prompt, extra_kwargs, compliance_check)

    llama.cpp はサーバー管理者が用意する任意機能。エンドポイント URL は
    アプリ config の `LLAMA_CPP_URL` から取得する。未設定なら llama.cpp 設定
    の既存ユーザーには「サーバー管理者が提供を停止した」と明確にエラーを返す。
    """
    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    if not config:
        raise ValueError("AI API設定が登録されていません。設定画面で登録してください。")

    raw_key = decrypt_api_key(config.api_key_encrypted)
    # llama.cpp はダミーキー "_" で保存されるので空文字に戻す
    api_key = "" if raw_key == "_" else raw_key
    provider = config.provider
    model = config.model_name or PROVIDER_DEFAULTS.get(provider, "")
    custom_prompt = getattr(config, "custom_prompt", "") or ""
    compliance_check = getattr(config, "compliance_check", False)

    handler = _PROVIDER_HANDLERS.get(provider)
    if not handler:
        raise ValueError(f"未対応のAIプロバイダーです: {provider}")

    extra_kwargs = {}
    if provider == "llama_cpp":
        from flask import current_app
        url = (current_app.config.get("LLAMA_CPP_URL") or "").strip()
        if not url:
            raise ValueError(
                "サーバー管理者が llama.cpp の提供を停止しました。"
                "AI 設定画面で別のプロバイダーに変更してください。"
            )
        # サーバー提供 LLM (llama.cpp) は有償機能。BYOK 経由の外部プロバイダ
        # (openai / anthropic / google) は引き続き無償で利用可能。
        from app.services.entitlement import has_entitlement
        from app.models.user import User
        user = db.session.get(User, user_id)
        if user is None:
            raise ValueError("ユーザーが見つかりません。")
        if not has_entitlement(user, "paid_llm"):
            raise ValueError(
                "サーバー提供 LLM (llama.cpp) の利用には有償プランが必要です。"
                "ご自身の API キーで外部プロバイダー (OpenAI / Anthropic / Google) を"
                "利用する場合は無償で継続できます。"
            )
        extra_kwargs["base_url"] = url

    return api_key, provider, model, handler, custom_prompt, extra_kwargs, compliance_check


def _log_ai_usage(user_id, provider, model, feature, usage, latency_ms,
                  *, status="ok", http_status=None):
    """AI API 呼び出し記録を ai_usage_logs テーブルに INSERT する。

    DB 書き込み失敗は AI 呼び出し本体に波及させない（try/except + rollback）。
    プライバシー: プロンプト本文・レスポンス本文・API キーは保存しない。
    """
    try:
        from app.models.ai_usage_log import AIUsageLog
        from app.extensions import db
        in_t = (usage or {}).get("input_tokens")
        out_t = (usage or {}).get("output_tokens")
        total = None
        if in_t is not None or out_t is not None:
            total = (in_t or 0) + (out_t or 0)
        db.session.add(AIUsageLog(
            user_id=user_id, provider=provider, model=model, feature=feature,
            input_tokens=in_t, output_tokens=out_t, total_tokens=total,
            latency_ms=latency_ms, status=status, http_status=http_status,
        ))
        db.session.commit()
    except Exception:
        logger.exception("Failed to record AI usage log for user %s", user_id)
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            pass


def _classify_exception(exc) -> tuple[str, int | None]:
    """例外を usage log の status / http_status に分類する。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_error", exc.response.status_code
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", None
    if isinstance(exc, (json.JSONDecodeError, KeyError, ValueError, TypeError)):
        return "parse_error", None
    return "other_error", None


def _call_ai(handler, api_key, model, image_bytes, mime_type,
             prompt, max_tokens, user_id, extra_kwargs=None,
             *, provider="", feature=""):
    """AI 画像 API 呼び出しの共通ラッパー。

    呼び出し前後で latency 計測 + 呼び出し記録を保存する。
    """
    import time as _time
    t0 = _time.perf_counter()
    try:
        kwargs = {"prompt": prompt, "max_tokens": max_tokens}
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        parsed, usage = handler(api_key, model, image_bytes, mime_type, **kwargs)
        latency = int((_time.perf_counter() - t0) * 1000)
        _log_ai_usage(user_id, provider, model, feature, usage, latency)
        return parsed
    except httpx.HTTPStatusError as e:
        latency = int((_time.perf_counter() - t0) * 1000)
        _log_ai_usage(user_id, provider, model, feature, {}, latency,
                      status="http_error", http_status=e.response.status_code)
        logger.error("AI API HTTP error for user %s: %s", user_id, e)
        raise RuntimeError(
            f"AI APIエラー（HTTP {e.response.status_code}）: "
            "APIキーやモデル名を確認してください。"
        )
    except Exception as e:
        latency = int((_time.perf_counter() - t0) * 1000)
        status, http_status = _classify_exception(e)
        _log_ai_usage(user_id, provider, model, feature, {}, latency,
                      status=status, http_status=http_status)
        logger.error("AI API call failed for user %s: %s", user_id, e)
        raise RuntimeError(f"AI APIの呼び出しに失敗しました: {e}")


def _call_ai_text(handler, api_key, model, prompt, max_tokens, user_id,
                  extra_kwargs=None, *, provider="", feature=""):
    """AI テキスト API 呼び出しの共通ラッパー (画像版と対称)。"""
    import time as _time
    t0 = _time.perf_counter()
    try:
        kwargs = {"max_tokens": max_tokens}
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        parsed, usage = handler(api_key, model, prompt, **kwargs)
        latency = int((_time.perf_counter() - t0) * 1000)
        _log_ai_usage(user_id, provider, model, feature, usage, latency)
        return parsed
    except httpx.HTTPStatusError as e:
        latency = int((_time.perf_counter() - t0) * 1000)
        _log_ai_usage(user_id, provider, model, feature, {}, latency,
                      status="http_error", http_status=e.response.status_code)
        logger.error("AI text API HTTP error for user %s: %s", user_id, e)
        raise RuntimeError(
            f"AI APIエラー（HTTP {e.response.status_code}）: "
            "APIキーやモデル名を確認してください。"
        )
    except Exception as e:
        latency = int((_time.perf_counter() - t0) * 1000)
        status, http_status = _classify_exception(e)
        _log_ai_usage(user_id, provider, model, feature, {}, latency,
                      status=status, http_status=http_status)
        logger.error("AI text API call failed for user %s: %s", user_id, e)
        raise RuntimeError(f"AI APIの呼び出しに失敗しました: {e}")


# E2 PR-C-4i: analyze_receipt / analyze_and_suggest / parse_web_text は
# E2-C-4e〜4h ですべての caller を削除済のため本 PR で削除。
# 残った _call_ai / _call_ai_text / _PROVIDER_HANDLERS / _TEXT_PROVIDER_HANDLERS
# は analyze_voucher_for_attachment (vouchers.py) / suggest_categories_by_ai
# (journal.py) / reconciliation 等のサーバ側 AI 機能で引き続き使用される。


AI_SUGGEST_CATEGORIES_PROMPT = """あなたは日本の複式簿記の家計簿アプリのアシスタントです。
以下は取込先口座「{payment_account_name}」の元帳（過去の取引履歴）です。
各行の「相手科目」は、その取引で使われた費目（勘定科目）です。

{ledger_context}

以下はユーザーの勘定科目一覧です。科目コードを使って回答してください。
{account_list}

以下の新規取引それぞれに、元帳のパターンを参考にして最も適切な勘定科目コードを推定してください。
- 出金は通常「費用」科目、入金は通常「収益」科目ですが、振替（資産・負債間の移動）の場合もあります。
- 元帳に似た摘要の取引がある場合は、その相手科目と同じ科目を優先してください。
- 該当なしの場合は account_code を null にしてください。

{rows_text}

必ず以下のJSON形式のみを返してください。余計なテキストは不要です。
{{"results": [{{"index": 0, "account_code": "科目コード"}}, ...]}}"""


def _get_payment_ledger_context(user_id: int, payment_account_code: str,
                                limit: int = 100) -> str:
    """支払口座の元帳データをテキスト形式で返す（相手科目名つき）"""
    account = Account.query.filter_by(
        user_id=user_id, code=payment_account_code
    ).first()
    if not account:
        return ""

    entries = (
        db.session.query(
            JournalEntry.date,
            JournalEntry.description,
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
            JournalEntry.id.label("entry_id"),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            JournalEntryLine.account_user_id == user_id,
            JournalEntryLine.account_code == payment_account_code,
            JournalEntry.user_id == user_id,
        )
        .order_by(JournalEntry.date.desc())
        .limit(limit)
        .all()
    )

    if not entries:
        return "(元帳データなし)"

    lines = [f"【{account.name}】の元帳（直近{len(entries)}件）"]
    lines.append("日付 | 摘要 | 相手科目 | 入金 | 出金")
    lines.append("-" * 60)

    for e in entries:
        counter_lines = (
            JournalEntryLine.query
            .filter(
                JournalEntryLine.journal_entry_id == e.entry_id,
                JournalEntryLine.account_code != payment_account_code,
            )
            .all()
        )
        counter_names = ", ".join(
            a.account.name for a in counter_lines if a.account
        ) if counter_lines else "?"

        d = int(e.debit_amount)
        c = int(e.credit_amount)
        lines.append(
            f"{e.date} | {e.description} | {counter_names} | "
            f"{'¥' + f'{d:,}' if d else '-'} | "
            f"{'¥' + f'{c:,}' if c else '-'}"
        )

    return "\n".join(lines)


def suggest_categories_by_ai(user_id: int, payment_account_code: str,
                              rows: list[dict]) -> dict:
    """元帳データをAIに渡して科目を推定する

    Args:
        rows: [{"description": "...", "deposit": 0, "withdrawal": 5000}, ...]

    Returns:
        {"摘要": {"account_code": "XXXX", "account_name": "..."}, ...}
    """
    api_key, provider, model, _, __, extra_kw, ___ = _get_ai_config(user_id)
    text_handler = _TEXT_PROVIDER_HANDLERS.get(provider)
    if not text_handler:
        raise ValueError(f"未対応のAIプロバイダーです: {provider}")

    account = Account.query.filter_by(
        user_id=user_id, code=payment_account_code
    ).first()
    payment_name = account.name if account else "不明"

    ledger_context = _get_payment_ledger_context(user_id, payment_account_code)
    account_list = _get_account_list_text(user_id)

    rows_lines = []
    for i, row in enumerate(rows):
        dep = row.get("deposit", 0)
        wd = row.get("withdrawal", 0)
        rows_lines.append(
            f"{i}. {row.get('description', '')} "
            f"(入金: ¥{dep:,}, 出金: ¥{wd:,})"
        )
    rows_text = "\n".join(rows_lines)

    prompt = AI_SUGGEST_CATEGORIES_PROMPT.format(
        payment_account_name=payment_name,
        ledger_context=ledger_context,
        account_list=account_list,
        rows_text=rows_text,
    )

    result = _call_ai_text(
        text_handler, api_key, model, prompt, 4000, user_id, extra_kw,
        provider=provider, feature="category_suggest",
    )

    # レスポンスを既存の suggest_categories と同じ形式に変換
    ai_results = result.get("results", [])
    accounts_cache = {}
    output = {}

    for item in ai_results:
        idx = item.get("index")
        acode = item.get("account_code")
        if idx is None or acode is None or idx >= len(rows):
            continue
        acode = str(acode)
        desc = rows[idx].get("description", "")
        if not desc or desc in output:
            continue

        if acode not in accounts_cache:
            acct = Account.query.filter_by(
                code=acode, user_id=user_id, is_active=True
            ).first()
            accounts_cache[acode] = acct

        acct = accounts_cache.get(acode)
        if acct:
            output[desc] = {"account_code": acct.code, "account_name": acct.name}

    return output


def match_account(user_id: int, category_name: str) -> str | None:
    """AI推測カテゴリ名をユーザーの勘定科目コードにマッチング"""
    expense_type = AccountType.query.filter_by(code="expense").first()
    if not expense_type:
        return None

    account = Account.query.filter(
        Account.user_id == user_id,
        Account.is_active.is_(True),
        Account.account_type_id == expense_type.id,
        Account.name == category_name,
    ).first()
    if account:
        return account.code

    expense_accounts = Account.query.filter(
        Account.user_id == user_id,
        Account.is_active.is_(True),
        Account.account_type_id == expense_type.id,
    ).order_by(Account.code).all()

    for acct in expense_accounts:
        if category_name in acct.name or acct.name in category_name:
            return acct.code

    if expense_accounts:
        return expense_accounts[0].code

    return None


# E2 PR-C-6a: analyze_voucher_for_attachment は E2EE 化に伴い削除。
# クライアント側 voucher_attach_orchestrator.js が等価の処理を実行する。
# 旧 CONSISTENCY_CHECK_PROMPT (Python str.format) もここでは使われなくなり、
# クライアント側用の CONSISTENCY_CHECK_PROMPT_TEMPLATE (placeholder 版) が
# /api/v1/voucher-attach/prompt-context から配信される。
