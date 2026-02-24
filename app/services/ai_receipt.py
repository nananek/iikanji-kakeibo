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
class ReceiptData:
    """AI解析結果（後方互換）"""

    date: str | None
    description: str
    amount: int
    suggested_category: str
    raw_response: dict


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
    "ollama": "llama3.2-vision",
}

PROVIDER_LABELS = {
    "openai": "OpenAI (GPT-4o)",
    "google": "Google Gemini",
    "anthropic": "Anthropic Claude",
    "ollama": "Ollama (ローカル)",
}

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
        {{"account_id": 勘定科目ID, "account_name": "勘定科目名", "debit_amount": 借方金額, "credit_amount": 0}},
        {{"account_id": 勘定科目ID, "account_name": "勘定科目名", "debit_amount": 0, "credit_amount": 貸方金額}}
      ]
    }}
  ]
}}
{ledger_section}
利用可能な勘定科目一覧（この中のIDと名前を使ってください）：
{account_list_text}
{custom_section}
注意事項:
- 各仕訳案の借方合計と貸方合計は必ず一致させてください
- account_id は上記一覧のIDを使ってください
- 金額は整数で返してください
- 給与明細等の複雑な書類では、詳細な内訳を反映した案と簡略化した案の両方を含めてください
- 異なる解釈（例: 費目の分類の違い、内訳の粒度の違い）を反映した複数案を提案してください"""


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
                 max_tokens: int = 500) -> dict:
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
    content = response.json()["choices"][0]["message"]["content"]
    return _extract_json(content)


def _call_google(api_key: str, model: str, image_bytes: bytes,
                 mime_type: str, prompt: str = RECEIPT_PROMPT,
                 max_tokens: int = 500) -> dict:
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
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text)


def _call_anthropic(api_key: str, model: str, image_bytes: bytes,
                    mime_type: str, prompt: str = RECEIPT_PROMPT,
                    max_tokens: int = 500) -> dict:
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
    content = response.json()["content"][0]["text"]
    return _extract_json(content)


def _call_ollama(api_key: str, model: str, image_bytes: bytes,
                 mime_type: str, prompt: str = RECEIPT_PROMPT,
                 max_tokens: int = 500, *, base_url: str = "") -> dict:
    url = (base_url.rstrip("/") if base_url else "http://localhost:11434")
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
    content = response.json()["choices"][0]["message"]["content"]
    return _extract_json(content)


_PROVIDER_HANDLERS = {
    "openai": _call_openai,
    "google": _call_google,
    "anthropic": _call_anthropic,
    "ollama": _call_ollama,
}


# --- テキスト専用プロバイダー呼出し ---


def _call_openai_text(api_key: str, model: str, prompt: str,
                      max_tokens: int = 2000) -> dict:
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
    content = response.json()["choices"][0]["message"]["content"]
    return _extract_json(content)


def _call_google_text(api_key: str, model: str, prompt: str,
                      max_tokens: int = 2000) -> dict:
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
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text)


def _call_anthropic_text(api_key: str, model: str, prompt: str,
                         max_tokens: int = 2000) -> dict:
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
    content = response.json()["content"][0]["text"]
    return _extract_json(content)


def _call_ollama_text(api_key: str, model: str, prompt: str,
                      max_tokens: int = 2000, *, base_url: str = "") -> dict:
    url = (base_url.rstrip("/") if base_url else "http://localhost:11434")
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
    content = response.json()["choices"][0]["message"]["content"]
    return _extract_json(content)


_TEXT_PROVIDER_HANDLERS = {
    "openai": _call_openai_text,
    "google": _call_google_text,
    "anthropic": _call_anthropic_text,
    "ollama": _call_ollama_text,
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
                JournalEntryLine.account_id == acct.id,
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
                .filter(JournalEntryLine.account_id == acct.id)
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

        lines.append(f"  ID:{acct.id} {acct.code} {acct.name}")

    return "\n".join(lines)


# --- メイン関数 ---


def _get_ai_config(user_id: int):
    """AI設定を取得してバリデーション

    Returns:
        (api_key, provider, model, handler, custom_prompt, base_url)
    """
    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    if not config:
        raise ValueError("AI API設定が登録されていません。設定画面で登録してください。")

    raw_key = decrypt_api_key(config.api_key_encrypted)
    # Ollama はダミーキー "_" で保存されるので空文字に戻す
    api_key = "" if raw_key == "_" else raw_key
    provider = config.provider
    model = config.model_name or PROVIDER_DEFAULTS.get(provider, "")
    custom_prompt = getattr(config, "custom_prompt", "") or ""
    base_url = getattr(config, "base_url", "") or ""

    handler = _PROVIDER_HANDLERS.get(provider)
    if not handler:
        raise ValueError(f"未対応のAIプロバイダーです: {provider}")

    return api_key, provider, model, handler, custom_prompt, base_url


def _call_ai(handler, api_key, model, image_bytes, mime_type,
             prompt, max_tokens, user_id, *, base_url=""):
    """AI APIを呼び出す共通ラッパー"""
    try:
        kwargs = {"prompt": prompt, "max_tokens": max_tokens}
        if base_url:
            kwargs["base_url"] = base_url
        return handler(api_key, model, image_bytes, mime_type, **kwargs)
    except httpx.HTTPStatusError as e:
        logger.error("AI API HTTP error for user %s: %s", user_id, e)
        raise RuntimeError(
            f"AI APIエラー（HTTP {e.response.status_code}）: "
            "APIキーやモデル名を確認してください。"
        )
    except Exception as e:
        logger.error("AI API call failed for user %s: %s", user_id, e)
        raise RuntimeError(f"AI APIの呼び出しに失敗しました: {e}")


def analyze_receipt(user_id: int, image_bytes: bytes,
                    mime_type: str) -> ReceiptData:
    """領収書画像をAIで解析する（後方互換）"""
    api_key, provider, model, handler, _, base_url = _get_ai_config(user_id)
    result = _call_ai(handler, api_key, model, image_bytes, mime_type,
                      RECEIPT_PROMPT, 500, user_id, base_url=base_url)

    return ReceiptData(
        date=result.get("date"),
        description=result.get("description", ""),
        amount=int(result.get("amount", 0)),
        suggested_category=result.get("category", "雑費"),
        raw_response=result,
    )


def analyze_and_suggest(user_id: int, image_bytes: bytes,
                        mime_type: str,
                        comment: str = "") -> list[JournalSuggestion]:
    """証憑画像を解析し、複数の仕訳案を生成する

    1. 第1ラウンド: 書類解析（種別判定、内訳抽出、元帳参照要否判定）
    2. 必要に応じて元帳データ取得
    3. 第2ラウンド: 勘定科目一覧+元帳データを踏まえた仕訳案生成

    Returns:
        list[JournalSuggestion]: 2〜4件の仕訳案

    Raises:
        ValueError: AI設定未登録またはプロバイダー未対応
        RuntimeError: API呼出し失敗
    """
    api_key, provider, model, handler, custom_prompt, base_url = _get_ai_config(user_id)

    # 第1ラウンド: 書類解析
    prompt = DOCUMENT_PROMPT
    if custom_prompt:
        prompt += f"\n\n## ユーザー定型情報\n{custom_prompt}"
    if comment:
        prompt += f"\n\nユーザーからのコメント: {comment}"
    round1 = _call_ai(handler, api_key, model, image_bytes, mime_type,
                      prompt, 1000, user_id, base_url=base_url)

    analysis = DocumentAnalysis(
        date=round1.get("date"),
        description=round1.get("description", ""),
        amount=int(round1.get("amount", 0)),
        document_type=round1.get("document_type", "other"),
        items=round1.get("items", []),
        needs_ledger=round1.get("needs_ledger", False),
        requested_accounts=round1.get("requested_accounts", []),
    )

    # 元帳データ取得（必要な場合）
    ledger_text = ""
    if analysis.needs_ledger and analysis.requested_accounts:
        ledger_text = _get_ledger_context(user_id, analysis.requested_accounts)

    # 第2ラウンド: 仕訳案生成
    account_list_text = _get_account_list_text(user_id)
    suggestion_prompt = _build_suggestion_prompt(
        account_list_text, ledger_text, custom_prompt
    )

    round2 = _call_ai(handler, api_key, model, image_bytes, mime_type,
                      suggestion_prompt, 2000, user_id, base_url=base_url)

    suggestions_raw = round2.get("suggestions", [])

    # バリデーション: account_idが実在するか確認
    valid_ids = {
        a.id for a in
        Account.query.filter_by(user_id=user_id, is_active=True).all()
    }

    suggestions = []
    for s in suggestions_raw:
        lines = []
        for line in s.get("lines", []):
            aid = int(line.get("account_id", 0))
            if aid in valid_ids:
                lines.append({
                    "account_id": aid,
                    "account_name": line.get("account_name", ""),
                    "debit_amount": int(line.get("debit_amount", 0)),
                    "credit_amount": int(line.get("credit_amount", 0)),
                })

        if lines:
            suggestions.append(JournalSuggestion(
                title=s.get("title", "仕訳案"),
                description=s.get("description", ""),
                date=s.get("date") or analysis.date,
                entry_description=s.get("entry_description", analysis.description),
                lines=lines,
            ))

    if not suggestions:
        raise RuntimeError(
            "AIが有効な仕訳案を生成できませんでした。"
            "画像を確認して再度お試しください。"
        )

    return suggestions


WEB_IMPORT_PROMPT = """あなたは日本の家計簿アプリのアシスタントです。
以下はユーザーが銀行・クレジットカード・証券口座などのWebページからコピーしたテキストです。
このテキストから取引明細を読み取り、以下のJSON形式で返してください。

必ず以下の形式のJSONのみを返してください。余計なテキストは不要です。

{{
  "transactions": [
    {{
      "date": "YYYY-MM-DD形式の日付",
      "description": "取引内容・摘要",
      "deposit": 入金額（整数、なければ0）,
      "withdrawal": 出金額（整数、なければ0）
    }}
  ]
}}

注意事項:
- 金額は整数で返してください（カンマ・円記号は除去）
- 入金（収入・預入・振込入金等）は deposit に、出金（支出・引落・振込出金等）は withdrawal に入れてください
- 日付が読み取れない行はスキップせず、date を null にしてください
- ヘッダー行や合計行、残高行は含めないでください
- 明細の順序はテキストに現れる順序のまま返してください
- クレジットカード明細の場合、利用金額は withdrawal として扱ってください

取込先口座: {payment_account_name}

--- Webページのテキスト ---
{raw_text}"""


def parse_web_text(user_id: int, raw_text: str,
                   payment_account_name: str) -> list[dict]:
    """Webページのテキストからai経由で明細を抽出する"""
    api_key, provider, model, _, __, base_url = _get_ai_config(user_id)

    text_handler = _TEXT_PROVIDER_HANDLERS.get(provider)
    if not text_handler:
        raise ValueError(f"未対応のAIプロバイダーです: {provider}")

    prompt = WEB_IMPORT_PROMPT.format(
        payment_account_name=payment_account_name,
        raw_text=raw_text[:50000],  # トークン上限を考慮して切り詰め
    )

    try:
        kwargs = {"max_tokens": 16000}
        if base_url:
            kwargs["base_url"] = base_url
        result = text_handler(api_key, model, prompt, **kwargs)
    except httpx.HTTPStatusError as e:
        logger.error("AI API HTTP error for user %s: %s", user_id, e)
        raise RuntimeError(
            f"AI APIエラー（HTTP {e.response.status_code}）: "
            "APIキーやモデル名を確認してください。"
        )
    except Exception as e:
        logger.error("AI API call failed for user %s: %s", user_id, e)
        raise RuntimeError(f"AI APIの呼び出しに失敗しました: {e}")

    transactions = result.get("transactions", [])

    parsed = []
    for i, tx in enumerate(transactions):
        parsed.append({
            "row_num": i + 1,
            "date": tx.get("date"),
            "description": tx.get("description", ""),
            "deposit": int(tx.get("deposit", 0)),
            "withdrawal": int(tx.get("withdrawal", 0)),
        })

    return parsed


AI_SUGGEST_CATEGORIES_PROMPT = """あなたは日本の複式簿記の家計簿アプリのアシスタントです。
以下は取込先口座「{payment_account_name}」の元帳（過去の取引履歴）です。
各行の「相手科目」は、その取引で使われた費目（勘定科目）です。

{ledger_context}

以下はユーザーの勘定科目一覧です。科目IDを使って回答してください。
{account_list}

以下の新規取引それぞれに、元帳のパターンを参考にして最も適切な勘定科目IDを推定してください。
- 出金は通常「費用」科目、入金は通常「収益」科目ですが、振替（資産・負債間の移動）の場合もあります。
- 元帳に似た摘要の取引がある場合は、その相手科目と同じ科目を優先してください。
- 該当なしの場合は account_id を null にしてください。

{rows_text}

必ず以下のJSON形式のみを返してください。余計なテキストは不要です。
{{"results": [{{"index": 0, "account_id": 科目ID}}, ...]}}"""


def _get_payment_ledger_context(user_id: int, account_id: int,
                                limit: int = 100) -> str:
    """支払口座の元帳データをテキスト形式で返す（相手科目名つき）"""
    account = Account.query.get(account_id)
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
            JournalEntryLine.account_id == account_id,
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
                JournalEntryLine.account_id != account_id,
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


def suggest_categories_by_ai(user_id: int, payment_account_id: int,
                              rows: list[dict]) -> dict:
    """元帳データをAIに渡して科目を推定する

    Args:
        rows: [{"description": "...", "deposit": 0, "withdrawal": 5000}, ...]

    Returns:
        {"摘要": {"account_id": N, "account_name": "..."}, ...}
    """
    api_key, provider, model, _, __, base_url = _get_ai_config(user_id)
    text_handler = _TEXT_PROVIDER_HANDLERS.get(provider)
    if not text_handler:
        raise ValueError(f"未対応のAIプロバイダーです: {provider}")

    account = Account.query.get(payment_account_id)
    payment_name = account.name if account else "不明"

    ledger_context = _get_payment_ledger_context(user_id, payment_account_id)
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

    try:
        kwargs = {"max_tokens": 4000}
        if base_url:
            kwargs["base_url"] = base_url
        result = text_handler(api_key, model, prompt, **kwargs)
    except httpx.HTTPStatusError as e:
        logger.error("AI API HTTP error for user %s: %s", user_id, e)
        raise RuntimeError(
            f"AI APIエラー（HTTP {e.response.status_code}）: "
            "APIキーやモデル名を確認してください。"
        )
    except Exception as e:
        logger.error("AI API call failed for user %s: %s", user_id, e)
        raise RuntimeError(f"AI APIの呼び出しに失敗しました: {e}")

    # レスポンスを既存の suggest_categories と同じ形式に変換
    ai_results = result.get("results", [])
    accounts_cache = {}
    output = {}

    for item in ai_results:
        idx = item.get("index")
        aid = item.get("account_id")
        if idx is None or aid is None or idx >= len(rows):
            continue
        desc = rows[idx].get("description", "")
        if not desc or desc in output:
            continue

        if aid not in accounts_cache:
            acct = Account.query.filter_by(
                id=aid, user_id=user_id, is_active=True
            ).first()
            accounts_cache[aid] = acct

        acct = accounts_cache.get(aid)
        if acct:
            output[desc] = {"account_id": acct.id, "account_name": acct.name}

    return output


def match_account(user_id: int, category_name: str) -> int | None:
    """AI推測カテゴリ名をユーザーの勘定科目IDにマッチング"""
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
        return account.id

    expense_accounts = Account.query.filter(
        Account.user_id == user_id,
        Account.is_active.is_(True),
        Account.account_type_id == expense_type.id,
    ).order_by(Account.code).all()

    for acct in expense_accounts:
        if category_name in acct.name or acct.name in category_name:
            return acct.id

    if expense_accounts:
        return expense_accounts[0].id

    return None
