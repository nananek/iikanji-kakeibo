"""AI証憑読取サービス - 領収書画像からの仕訳データ抽出"""

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass

import httpx
from cryptography.fernet import Fernet
from flask import current_app

from app.models.account import Account, AccountType
from app.models.ai_config import UserAIConfig

logger = logging.getLogger(__name__)


@dataclass
class ReceiptData:
    """AI解析結果"""

    date: str | None
    description: str
    amount: int
    suggested_category: str
    raw_response: dict


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
    """暗号化されたAPIキーを復号"""
    return _get_fernet().decrypt(encrypted).decode()


# --- プロバイダー設定 ---

PROVIDER_DEFAULTS = {
    "openai": "gpt-4o",
    "google": "gemini-2.0-flash",
    "anthropic": "claude-sonnet-4-20250514",
}

PROVIDER_LABELS = {
    "openai": "OpenAI (GPT-4o)",
    "google": "Google Gemini",
    "anthropic": "Anthropic Claude",
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


def _extract_json(text: str) -> dict:
    """テキストからJSON部分を抽出してパース"""
    text = text.strip()
    # まずそのままパースを試行
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # ```json ... ``` ブロックを探す
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # { ... } を探す
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise json.JSONDecodeError("JSONが見つかりません", text, 0)


# --- プロバイダー別API呼出し ---


def _call_openai(api_key: str, model: str, image_bytes: bytes,
                 mime_type: str) -> dict:
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
                        {"type": "text", "text": RECEIPT_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{b64_image}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 500,
        },
        timeout=60.0,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return _extract_json(content)


def _call_google(api_key: str, model: str, image_bytes: bytes,
                 mime_type: str) -> dict:
    b64_image = base64.b64encode(image_bytes).decode()
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={
            "contents": [
                {
                    "parts": [
                        {"text": RECEIPT_PROMPT},
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
            },
        },
        timeout=60.0,
    )
    response.raise_for_status()
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text)


def _call_anthropic(api_key: str, model: str, image_bytes: bytes,
                    mime_type: str) -> dict:
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
            "max_tokens": 500,
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
                        {"type": "text", "text": RECEIPT_PROMPT},
                    ],
                }
            ],
        },
        timeout=60.0,
    )
    response.raise_for_status()
    content = response.json()["content"][0]["text"]
    return _extract_json(content)


_PROVIDER_HANDLERS = {
    "openai": _call_openai,
    "google": _call_google,
    "anthropic": _call_anthropic,
}


# --- メイン関数 ---


def analyze_receipt(user_id: int, image_bytes: bytes,
                    mime_type: str) -> ReceiptData:
    """領収書画像をAIで解析する

    Raises:
        ValueError: AI設定未登録またはプロバイダー未対応
        RuntimeError: API呼出し失敗
    """
    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    if not config:
        raise ValueError("AI API設定が登録されていません。設定画面で登録してください。")

    api_key = decrypt_api_key(config.api_key_encrypted)
    provider = config.provider
    model = config.model_name or PROVIDER_DEFAULTS.get(provider, "")

    handler = _PROVIDER_HANDLERS.get(provider)
    if not handler:
        raise ValueError(f"未対応のAIプロバイダーです: {provider}")

    try:
        result = handler(api_key, model, image_bytes, mime_type)
    except httpx.HTTPStatusError as e:
        logger.error("AI API HTTP error for user %s: %s", user_id, e)
        raise RuntimeError(
            f"AI APIエラー（HTTP {e.response.status_code}）: "
            "APIキーやモデル名を確認してください。"
        )
    except Exception as e:
        logger.error("AI API call failed for user %s: %s", user_id, e)
        raise RuntimeError(f"AI APIの呼び出しに失敗しました: {e}")

    return ReceiptData(
        date=result.get("date"),
        description=result.get("description", ""),
        amount=int(result.get("amount", 0)),
        suggested_category=result.get("category", "雑費"),
        raw_response=result,
    )


def match_account(user_id: int, category_name: str) -> int | None:
    """AI推測カテゴリ名をユーザーの勘定科目IDにマッチング"""
    expense_type = AccountType.query.filter_by(code="expense").first()
    if not expense_type:
        return None

    # 完全一致
    account = Account.query.filter(
        Account.user_id == user_id,
        Account.is_active.is_(True),
        Account.account_type_id == expense_type.id,
        Account.name == category_name,
    ).first()
    if account:
        return account.id

    # 部分一致
    expense_accounts = Account.query.filter(
        Account.user_id == user_id,
        Account.is_active.is_(True),
        Account.account_type_id == expense_type.id,
    ).order_by(Account.code).all()

    for acct in expense_accounts:
        if category_name in acct.name or acct.name in category_name:
            return acct.id

    # フォールバック: 最初の費用勘定
    if expense_accounts:
        return expense_accounts[0].id

    return None
