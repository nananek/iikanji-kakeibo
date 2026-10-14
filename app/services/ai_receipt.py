"""AI証憑読取サービス - 証憑画像からの仕訳データ抽出"""

import base64
import json
import logging
import re
from dataclasses import dataclass, field

import httpx
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
    """SECRET_KEY から Fernet インスタンスを生成 (共通ヘルパーへ委譲)"""
    from app.services.crypto import get_fernet
    return get_fernet()


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


def resolve_custom_headers(raw_headers_json: str, user_id: int) -> dict[str, str]:
    """カスタムヘッダーJSON文字列をパースし、動的プレースホルダーを展開する。

    サポートするプレースホルダー:
    - {IK_SESSION_ID}: FlaskセッションID（利用できない場合はUUID）
    - {USER_ID}: ユーザーID
    """
    if not raw_headers_json or not raw_headers_json.strip():
        return {}

    try:
        headers_dict = json.loads(raw_headers_json)
        if not isinstance(headers_dict, dict):
            return {}
    except Exception:
        logger.warning("Failed to parse custom_headers JSON for user %s", user_id)
        return {}

    session_id = ""
    try:
        from flask import session as flask_session, has_request_context
        if has_request_context() and flask_session:
            session_id = flask_session.get("_id") or flask_session.get("csrf_token") or ""
    except Exception:
        pass
    if not session_id:
        import uuid
        session_id = str(uuid.uuid4())

    forbidden = {"host", "content-length", "transfer-encoding", "connection"}
    resolved = {}
    for k, v in headers_dict.items():
        key_str = str(k).strip()
        if key_str.lower() in forbidden:
            continue
        val_str = str(v)
        val_str = val_str.replace("{IK_SESSION_ID}", session_id)
        val_str = val_str.replace("{USER_ID}", str(user_id))
        resolved[key_str] = val_str

    return resolved


# --- プロバイダー設定 ---

PROVIDER_DEFAULTS = {
    "openai": "gpt-4o",
    "google": "gemini-2.0-flash",
    "anthropic": "claude-sonnet-4-20250514",
    "llama_cpp": "default",
    "openai_compatible": "gpt-4o",
}

PROVIDER_LABELS = {
    "openai": "OpenAI (GPT-4o)",
    "google": "Google Gemini",
    "anthropic": "Anthropic Claude",
    "llama_cpp": "llama.cpp (サーバー提供)",
    "openai_compatible": "OpenAI 互換 API (カスタムエンドポイント)",
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
    openai_compatible はユーザー自由設定のため常に含める。
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
    """OpenAI 互換 (OpenAI / llama.cpp) のレスポンスから usage を正規化。

    Chat Completions は prompt_tokens / completion_tokens、Responses API は
    input_tokens / output_tokens を返すため、両方を吸収する。
    """
    usage = data.get("usage") or {}
    return {
        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
    }


def _raise_for_status(response) -> None:
    """HTTP エラー時にレスポンス本文をメッセージへ含める raise_for_status。

    400 等のクライアントエラーは API 側の説明本文に原因が書かれていることが
    多いが、httpx の既定メッセージには含まれない。診断のため本文を添えて
    同じ HTTPStatusError を投げ直す（呼び出し側の status_code 分類は維持）。
    """
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = " ".join((e.response.text or "").split())[:500]
        if not body:
            raise
        raise httpx.HTTPStatusError(
            f"{e} — response body: {body}",
            request=e.request,
            response=e.response,
        ) from None


def _extract_responses_text(data: dict) -> str:
    """Responses API のレスポンスから生成テキストを抽出する。

    `output_text` ショートハンドがあればそれを優先し、なければ output の
    message アイテム（content は output_text 型）を連結する。
    """
    text = data.get("output_text")
    if isinstance(text, str) and text:
        return text
    parts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                parts.append(c.get("text", "") or "")
    return "".join(parts)


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


def _call_openai_compatible(api_key: str, model: str, image_bytes: bytes,
                            mime_type: str, prompt: str = RECEIPT_PROMPT,
                            max_tokens: int = 500, *, base_url: str = "",
                            headers: dict = None, timeout: float = 60.0,
                            api_style: str = "chat_completions", **kwargs):
    """OpenAI 互換 API (カスタムエンドポイント) を呼ぶ。

    ユーザー指定の base_url (OpenCode Go 等) に対応。API キーはユーザー各自
    (BYOK) が指定する。llama.cpp もこの実装へ委譲する。
    api_style が "responses" の場合は Responses API (/v1/responses) を使う。
    戻り値: (parsed_json, usage_dict)
    """
    url = base_url.rstrip("/")
    b64_image = base64.b64encode(image_bytes).decode()
    req_headers = {"Content-Type": "application/json"}
    if api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        req_headers.update(headers)

    if api_style == "responses":
        response = httpx.post(
            f"{url}/v1/responses",
            headers=req_headers,
            json={
                "model": model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {
                                "type": "input_image",
                                "image_url": f"data:{mime_type};base64,{b64_image}",
                            },
                        ],
                    }
                ],
                "max_output_tokens": max_tokens,
            },
            timeout=timeout,
        )
        _raise_for_status(response)
        data = response.json()
        return _extract_json(_extract_responses_text(data)), _usage_openai_style(data)

    response = httpx.post(
        f"{url}/v1/chat/completions",
        headers=req_headers,
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
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _extract_json(content), _usage_openai_style(data)


def _call_llama_cpp(api_key: str, model: str, image_bytes: bytes,
                    mime_type: str, prompt: str = RECEIPT_PROMPT,
                    max_tokens: int = 500, *, base_url: str = ""):
    """llama.cpp (llama-server) の OpenAI 互換エンドポイントを呼ぶ。

    デフォルトポートは 8080。マルチモーダル対応モデル (LLaVA など) が必要。
    OpenAI 互換 API の共通コア (_call_openai_compatible) へ委譲する。
    戻り値: (parsed_json, usage_dict)
    """
    return _call_openai_compatible(
        api_key, model, image_bytes, mime_type, prompt, max_tokens,
        base_url=(base_url or "http://localhost:8080"), timeout=120.0,
    )


_PROVIDER_HANDLERS = {
    "openai": _call_openai,
    "google": _call_google,
    "anthropic": _call_anthropic,
    "llama_cpp": _call_llama_cpp,
    "openai_compatible": _call_openai_compatible,
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


def _call_openai_compatible_text(api_key: str, model: str, prompt: str,
                                 max_tokens: int = 2000, *, base_url: str = "",
                                 headers: dict = None, timeout: float = 60.0,
                                 api_style: str = "chat_completions", **kwargs):
    """OpenAI 互換 API (カスタムエンドポイント) のテキスト専用呼び出し。

    llama.cpp のテキスト呼び出しもこの実装へ委譲する。
    api_style が "responses" の場合は Responses API (/v1/responses) を使う。
    戻り値: (parsed_json, usage_dict)
    """
    url = base_url.rstrip("/")
    req_headers = {"Content-Type": "application/json"}
    if api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        req_headers.update(headers)

    if api_style == "responses":
        response = httpx.post(
            f"{url}/v1/responses",
            headers=req_headers,
            json={
                "model": model,
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    }
                ],
                "max_output_tokens": max_tokens,
            },
            timeout=timeout,
        )
        _raise_for_status(response)
        data = response.json()
        return _extract_json(_extract_responses_text(data)), _usage_openai_style(data)

    response = httpx.post(
        f"{url}/v1/chat/completions",
        headers=req_headers,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _extract_json(content), _usage_openai_style(data)


def _call_llama_cpp_text(api_key: str, model: str, prompt: str,
                         max_tokens: int = 2000, *, base_url: str = ""):
    """llama.cpp (llama-server) のテキスト専用 OpenAI 互換呼び出し。

    OpenAI 互換 API の共通コア (_call_openai_compatible_text) へ委譲する。
    戻り値: (parsed_json, usage_dict)
    """
    return _call_openai_compatible_text(
        api_key, model, prompt, max_tokens,
        base_url=(base_url or "http://localhost:8080"), timeout=120.0,
    )


_TEXT_PROVIDER_HANDLERS = {
    "openai": _call_openai_text,
    "google": _call_google_text,
    "anthropic": _call_anthropic_text,
    "llama_cpp": _call_llama_cpp_text,
    "openai_compatible": _call_openai_compatible_text,
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
        (api_key, provider, model, handler, custom_prompt, extra_kwargs)

    llama.cpp はサーバー管理者が用意する任意機能。エンドポイント URL は
    アプリ config の `LLAMA_CPP_URL` から取得する。未設定なら llama.cpp 設定
    の既存ユーザーには「サーバー管理者が提供を停止した」と明確にエラーを返す。
    openai_compatible はユーザー設定の `base_url` を使う (未設定ならエラー)。
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
    elif provider == "openai_compatible":
        base_url = (getattr(config, "base_url", "") or "").strip()
        if not base_url:
            raise ValueError(
                "OpenAI 互換 API のベースURLが設定されていません。"
                "AI 設定画面でエンドポイントを入力してください。"
            )
        # BYOK の外部プロバイダー (openai / anthropic / google と同様) のため
        # 有償エンタイトルメントは不要。
        extra_kwargs["base_url"] = base_url
        extra_kwargs["api_style"] = getattr(config, "api_style", "chat_completions") or "chat_completions"
        raw_headers = getattr(config, "custom_headers", "") or ""
        extra_kwargs["headers"] = resolve_custom_headers(raw_headers, user_id)

    return api_key, provider, model, handler, custom_prompt, extra_kwargs


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


def analyze_receipt(user_id: int, image_bytes: bytes,
                    mime_type: str) -> ReceiptData:
    """領収書画像をAIで解析する（後方互換）"""
    api_key, provider, model, handler, _, extra_kw = _get_ai_config(user_id)
    result = _call_ai(handler, api_key, model, image_bytes, mime_type,
                      RECEIPT_PROMPT, 500, user_id, extra_kw,
                      provider=provider, feature="receipt_analyze")

    return ReceiptData(
        date=result.get("date"),
        description=result.get("description", ""),
        amount=int(result.get("amount", 0)),
        suggested_category=result.get("category", "雑費"),
        raw_response=result,
    )


# --- AIエージェント（ツールコール方式）---

DEFAULT_MAX_TURNS = 10
MIN_MAX_TURNS = 1
MAX_MAX_TURNS = 50

# 決定的フォールバックで使う既定科目の優先名。
# 前方一致で探し、無ければ同種別の先頭科目に落ちる。
_FALLBACK_EXPENSE_PREFERRED = ("雑費", "消耗品費", "日用品費")
_FALLBACK_PAYMENT_PREFERRED = ("現金", "普通預金")


def _clamp_max_turns(value) -> int:
    """設定値を [MIN_MAX_TURNS, MAX_MAX_TURNS] に丸める。不正値は既定値。"""
    try:
        turns = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TURNS
    return min(max(turns, MIN_MAX_TURNS), MAX_MAX_TURNS)


def _resolve_max_turns(user_id: int) -> int:
    """ユーザー設定のエージェント最大ターン数を返す (未設定は既定値)。"""
    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    if config is None:
        return DEFAULT_MAX_TURNS
    return _clamp_max_turns(getattr(config, "max_turns", None))


AGENT_SYSTEM_PROMPT = """あなたは日本の複式簿記の家計簿・経理アシスタントです。
提示された証憑（領収書、レシート、給与明細、請求書等）の画像から取引内容を読み取り、適切な仕訳案を2〜4件提案してください。

## あなたの行動規範:
1. まず画像から「日付」「取引内容（店名・支払先等）」「金額」「明細」を正確に読み取ってください。
2. 必要に応じて利用可能なツールを呼び出して調査・検証を行ってください:
   - search_accounts: キーワードや種別で実在する勘定科目コードを検索します。
   - search_journal_history: 取引先名や店名で過去の類似仕訳を検索し、過去の仕訳パターンを参照します。
   - get_account_ledger: 指定した複数科目の直近の元帳データや残高をまとめて確認します。
   - get_account_list: 科目体系全体を確認します。
3. 十分な調査が完了したら、必ず submit_journal_suggestions ツールを呼び出して2〜4件の仕訳案を提出してください。
   - 各仕訳案の借方合計と貸方合計は必ず一致させてください。
   - account_code は必ず実在するコードを指定してください。
   - 給与明細のように複雑な書類では、詳細な内訳を反映した案と簡略化した案の両方を含めてください。
   - 費目の解釈や按分の違いを反映した複数のアプローチを提案してください。
4. 調査は手早く済ませ、ターンを使い切る前に必ず仕訳案を提出してください。"""

AGENT_TOOLS = [
    {
        "name": "search_accounts",
        "description": "ユーザーの登録勘定科目をキーワード（科目名またはコード）や科目種別で検索します。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "検索する科目名やキーワード（例: 消耗品, 旅費, 普通預金）",
                },
                "account_type": {
                    "type": "string",
                    "enum": ["asset", "liability", "equity", "revenue", "expense"],
                    "description": "科目種別（任意）: asset(資産), liability(負債), equity(純資産), revenue(収益), expense(費用)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_account_list",
        "description": "勘定科目の一覧を取得します。全体の科目体系を確認したいときに使用します。",
        "parameters": {
            "type": "object",
            "properties": {
                "account_type": {
                    "type": "string",
                    "enum": ["asset", "liability", "equity", "revenue", "expense"],
                    "description": "特定種別のみ取得する場合に指定",
                },
            },
        },
    },
    {
        "name": "search_journal_history",
        "description": "過去の仕訳履歴から、取引先・店名・摘要などのキーワードに一致する仕訳を検索します。過去にどのような勘定科目で処理したか確認できます。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "検索する店名や取引内容（例: セブンイレブン, Amazon）",
                },
                "limit": {
                    "type": "integer",
                    "description": "最大取得件数（1〜10件）",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_account_ledger",
        "description": "指定した複数の勘定科目の直近の元帳データ（仕訳履歴、相手科目、残高）をまとめて取得します。",
        "parameters": {
            "type": "object",
            "properties": {
                "account_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "勘定科目コードのリスト（例: [\"6101\", \"1010\"]）。複数科目をまとめて確認できます。",
                },
                "limit": {
                    "type": "integer",
                    "description": "科目ごとの最大取得件数（1〜10件）",
                    "default": 5,
                },
            },
            "required": ["account_codes"],
        },
    },
    {
        "name": "submit_journal_suggestions",
        "description": "検討した仕訳案を確定して提出します。調査・検討が完了したら必ずこのツールを呼んで仕訳案（2〜4件）を提出してください。",
        "parameters": {
            "type": "object",
            "properties": {
                "suggestions": {
                    "type": "array",
                    "description": "仕訳案のリスト（2〜4件）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "仕訳案の簡潔なタイトル（例: 消耗品費として計上）",
                            },
                            "description": {
                                "type": "string",
                                "description": "この仕訳案の根拠や説明",
                            },
                            "date": {
                                "type": "string",
                                "description": "仕訳日付 (YYYY-MM-DD形式)",
                            },
                            "entry_description": {
                                "type": "string",
                                "description": "仕訳帳に記録する摘要（店名・取引内容）",
                            },
                            "lines": {
                                "type": "array",
                                "description": "仕訳の明細行（借方合計と貸方合計を必ず一致させてください）",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "account_code": {
                                            "type": "string",
                                            "description": "勘定科目コード",
                                        },
                                        "account_name": {
                                            "type": "string",
                                            "description": "勘定科目名",
                                        },
                                        "debit_amount": {
                                            "type": "integer",
                                            "description": "借方金額（貸方の場合は0）",
                                        },
                                        "credit_amount": {
                                            "type": "integer",
                                            "description": "貸方金額（借方の場合は0）",
                                        },
                                    },
                                    "required": ["account_code", "debit_amount", "credit_amount"],
                                },
                            },
                        },
                        "required": ["title", "description", "date", "entry_description", "lines"],
                    },
                },
            },
            "required": ["suggestions"],
        },
    },
]


def _execute_agent_tool(user_id: int, name: str, args: dict) -> dict:
    """エージェントから要求されたツールを実行する"""
    if name == "search_accounts":
        query = str(args.get("query") or "").strip()
        atype = str(args.get("account_type") or "").strip()
        q = Account.query.filter_by(user_id=user_id, is_active=True).join(AccountType)
        if atype:
            q = q.filter(AccountType.code == atype)
        if query:
            term = f"%{query}%"
            q = q.filter(Account.name.ilike(term) | Account.code.ilike(term))
        matched = q.order_by(AccountType.display_order, Account.code).limit(15).all()
        return {
            "accounts": [
                {
                    "code": a.code,
                    "name": a.name,
                    "type": a.account_type.code,
                    "type_name": a.account_type.name,
                }
                for a in matched
            ]
        }

    elif name == "get_account_list":
        atype = str(args.get("account_type") or "").strip()
        q = Account.query.filter_by(user_id=user_id, is_active=True).join(AccountType)
        if atype:
            q = q.filter(AccountType.code == atype)
        accounts = q.order_by(AccountType.display_order, Account.code).all()
        grouped = {}
        for a in accounts:
            label = a.account_type.name
            grouped.setdefault(label, []).append(f"{a.code} {a.name}")
        return {"accounts": grouped}

    elif name == "search_journal_history":
        query = str(args.get("query") or "").strip()
        limit = min(max(int(args.get("limit") or 5), 1), 10)
        q = JournalEntry.query.filter_by(user_id=user_id)
        if query:
            q = q.filter(JournalEntry.description.ilike(f"%{query}%"))
        entries = q.order_by(JournalEntry.date.desc()).limit(limit).all()
        accts = Account.query.filter_by(user_id=user_id, is_active=True).all()
        acct_map = {a.code: a.name for a in accts}
        results = []
        for e in entries:
            results.append({
                "date": str(e.date),
                "description": e.description,
                "lines": [
                    {
                        "account_code": l.account_code,
                        "account_name": acct_map.get(l.account_code, ""),
                        "debit_amount": int(l.debit_amount or 0),
                        "credit_amount": int(l.credit_amount or 0),
                    }
                    for l in e.lines
                ],
            })
        return {"entries": results}

    elif name == "get_account_ledger":
        # account_codes (複数) を主とし、後方互換で account_code (単一) も受ける。
        raw_codes = args.get("account_codes")
        if raw_codes is None:
            single = args.get("account_code")
            raw_codes = [single] if single else []
        if isinstance(raw_codes, str):
            raw_codes = [raw_codes]
        codes = []
        for c in raw_codes or []:
            code = str(c).strip()
            if code and code not in codes:
                codes.append(code)
        if not codes:
            return {"error": "account_codes を指定してください。"}

        limit = min(max(int(args.get("limit") or 5), 1), 10)
        all_accts = Account.query.filter_by(user_id=user_id, is_active=True).all()
        acct_map = {a.code: a.name for a in all_accts}

        accounts_out = []
        not_found = []
        for code in codes:
            if code not in acct_map:
                not_found.append(code)
                continue
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
                    JournalEntryLine.account_code == code,
                    JournalEntry.user_id == user_id,
                )
                .order_by(JournalEntry.date.desc())
                .limit(limit)
                .all()
            )
            lines = []
            for e in entries:
                counter_lines = (
                    JournalEntryLine.query.filter(
                        JournalEntryLine.journal_entry_id == e.entry_id,
                        JournalEntryLine.account_code != code,
                    ).all()
                )
                counter_names = ", ".join(
                    acct_map.get(l.account_code, l.account_code) for l in counter_lines
                ) if counter_lines else "?"
                lines.append({
                    "date": str(e.date),
                    "description": e.description,
                    "counter_account": counter_names,
                    "debit": int(e.debit_amount),
                    "credit": int(e.credit_amount),
                })
            accounts_out.append({
                "account_code": code,
                "account_name": acct_map[code],
                "entries": lines,
            })

        result = {"accounts": accounts_out}
        if not_found:
            result["not_found"] = not_found
        if not accounts_out:
            result["error"] = (
                "指定された勘定科目コードが見つかりません: " + ", ".join(not_found)
            )
        return result

    return {"error": f"未知のツールです: {name}"}


def _agent_step_openai(
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    *,
    base_url: str = "",
    headers: dict = None,
    timeout: float = 60.0,
    force_tool: str | None = None,
    **kwargs,
):
    """OpenAI / llama.cpp / OpenAI互換 (Chat Completions) の1ターン実行"""
    url = (base_url.rstrip("/") if base_url else "https://api.openai.com") + "/v1/chat/completions"
    req_headers = {"Content-Type": "application/json"}
    if api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        req_headers.update(headers)

    tool_choice = "auto"
    if force_tool:
        tool_choice = {"type": "function", "function": {"name": force_tool}}
    payload = {
        "model": model,
        "messages": messages,
        "tools": [{"type": "function", "function": t} for t in tools],
        "tool_choice": tool_choice,
        "max_tokens": 2000,
    }

    response = httpx.post(url, headers=req_headers, json=payload, timeout=timeout)
    _raise_for_status(response)
    data = response.json()
    msg = data["choices"][0]["message"]
    text = msg.get("content") or ""
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name")
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except Exception:
            args = {}
        tool_calls.append({
            "id": tc.get("id", f"call_{len(tool_calls)}"),
            "name": name,
            "arguments": args,
        })
    updated_messages = list(messages) + [msg]
    return text, tool_calls, updated_messages, _usage_openai_style(data)


def _agent_step_openai_responses(
    api_key: str,
    model: str,
    input_items: list[dict],
    tools: list[dict],
    *,
    base_url: str = "",
    headers: dict = None,
    timeout: float = 60.0,
    force_tool: str | None = None,
    **kwargs,
):
    """OpenAI 互換 Responses API (/v1/responses) の1ターン実行"""
    url = base_url.rstrip("/") + "/v1/responses"
    req_headers = {"Content-Type": "application/json"}
    if api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        req_headers.update(headers)

    resp_tools = [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        }
        for t in tools
    ]
    payload = {
        "model": model,
        "input": input_items,
        "tools": resp_tools,
        "max_output_tokens": 2000,
    }
    if force_tool:
        payload["tool_choice"] = {"type": "function", "name": force_tool}

    response = httpx.post(url, headers=req_headers, json=payload, timeout=timeout)
    _raise_for_status(response)
    data = response.json()

    text = _extract_responses_text(data)
    tool_calls = []
    updated_input = list(input_items)

    outputs = data.get("output") or []
    if isinstance(outputs, list):
        for item in outputs:
            updated_input.append(item)
            itype = item.get("type")
            if itype == "function_call":
                name = item.get("name")
                args_raw = item.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    args = {}
                tool_calls.append({
                    "id": item.get("call_id", f"call_{len(tool_calls)}"),
                    "name": name,
                    "arguments": args,
                })
    elif "choices" in data:
        msg = data["choices"][0]["message"]
        text = msg.get("content") or ""
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except Exception:
                args = {}
            tool_calls.append({
                "id": tc.get("id", f"call_{len(tool_calls)}"),
                "name": fn.get("name"),
                "arguments": args,
            })
        updated_input.append(msg)

    return text, tool_calls, updated_input, _usage_openai_style(data)


def _agent_step_anthropic(
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    *,
    system_prompt: str = "",
    timeout: float = 60.0,
    force_tool: str | None = None,
    **kwargs,
):
    """Anthropic Claude の1ターン実行"""
    url = "https://api.anthropic.com/v1/messages"
    req_headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    anthropic_tools = [
        {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
        for t in tools
    ]
    payload = {
        "model": model,
        "max_tokens": 2000,
        "messages": messages,
        "tools": anthropic_tools,
    }
    if system_prompt:
        payload["system"] = system_prompt
    if force_tool:
        payload["tool_choice"] = {"type": "tool", "name": force_tool}

    response = httpx.post(url, headers=req_headers, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    content_blocks = data.get("content", [])
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    tool_calls = []
    for b in content_blocks:
        if b.get("type") == "tool_use":
            tool_calls.append({
                "id": b.get("id", f"call_{len(tool_calls)}"),
                "name": b.get("name"),
                "arguments": b.get("input", {}),
            })
    updated_messages = list(messages) + [{"role": "assistant", "content": content_blocks}]
    return text, tool_calls, updated_messages, _usage_anthropic_style(data)


def _agent_step_google(
    api_key: str,
    model: str,
    contents: list[dict],
    tools: list[dict],
    *,
    system_prompt: str = "",
    timeout: float = 60.0,
    force_tool: str | None = None,
    **kwargs,
):
    """Google Gemini の1ターン実行"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    gemini_tools = [
        {"functionDeclarations": [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in tools]}
    ]
    payload = {
        "contents": contents,
        "tools": gemini_tools,
        "generationConfig": {"maxOutputTokens": 2000},
    }
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    if force_tool:
        payload["toolConfig"] = {
            "functionCallingConfig": {
                "mode": "ANY",
                "allowedFunctionNames": [force_tool],
            }
        }

    response = httpx.post(url, params={"key": api_key}, headers={"Content-Type": "application/json"}, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    candidate = data.get("candidates", [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if "text" in p)
    tool_calls = []
    for p in parts:
        if "functionCall" in p:
            fc = p["functionCall"]
            tool_calls.append({
                "id": fc.get("name", f"call_{len(tool_calls)}"),
                "name": fc.get("name"),
                "arguments": fc.get("args", {}),
            })
    updated_contents = list(contents) + [{"role": "model", "parts": parts}]
    return text, tool_calls, updated_contents, _usage_google_style(data)


_AGENT_STEP_HANDLERS = {
    "openai": _agent_step_openai,
    "llama_cpp": _agent_step_openai,
    "openai_compatible": _agent_step_openai,
    "anthropic": _agent_step_anthropic,
    "google": _agent_step_google,
}


def _append_tool_results_openai(messages: list[dict], results: list[tuple[str, dict]]) -> list[dict]:
    updated = list(messages)
    for call_id, res in results:
        updated.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(res, ensure_ascii=False),
        })
    return updated


def _append_tool_results_responses(input_items: list[dict], results: list[tuple[str, dict]]) -> list[dict]:
    updated = list(input_items)
    for call_id, res in results:
        updated.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(res, ensure_ascii=False),
        })
    return updated


def _append_tool_results_anthropic(messages: list[dict], results: list[tuple[str, dict]]) -> list[dict]:
    updated = list(messages)
    updated.append({
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": json.dumps(res, ensure_ascii=False),
            }
            for call_id, res in results
        ],
    })
    return updated


def _append_tool_results_google(contents: list[dict], results: list[tuple[str, dict]]) -> list[dict]:
    updated = list(contents)
    parts = []
    for _, (call_name, res) in results:
        parts.append({
            "functionResponse": {
                "name": call_name,
                "response": {"result": res},
            }
        })
    updated.append({"role": "user", "parts": parts})
    return updated


def _parse_suggestions_list(user_id: int, raw_list: list) -> list[JournalSuggestion]:
    """生データの仕訳案リストをバリデーションして JournalSuggestion に変換"""
    valid_codes = {
        a.code for a in Account.query.filter_by(user_id=user_id, is_active=True).all()
    }
    suggestions = []
    for s in raw_list:
        if not isinstance(s, dict):
            continue
        lines = []
        for line in s.get("lines", []):
            acode = str(line.get("account_code", ""))
            if acode in valid_codes:
                lines.append({
                    "account_code": acode,
                    "account_name": line.get("account_name", ""),
                    "debit_amount": int(line.get("debit_amount", 0)),
                    "credit_amount": int(line.get("credit_amount", 0)),
                })
        if lines:
            suggestions.append(JournalSuggestion(
                title=s.get("title", "仕訳案"),
                description=s.get("description", ""),
                date=s.get("date"),
                entry_description=s.get("entry_description", ""),
                lines=lines,
            ))
    return suggestions


def _fallback_extract_suggestions(text: str, user_id: int) -> list[JournalSuggestion]:
    """テキストからJSON仕訳案をフォールバック抽出"""
    try:
        data = _extract_json(text)
        if isinstance(data, dict) and "suggestions" in data:
            return _parse_suggestions_list(user_id, data["suggestions"])
        if isinstance(data, list):
            return _parse_suggestions_list(user_id, data)
    except Exception:
        pass
    return []


def _tool_progress(name: str, args: dict) -> tuple[str, str]:
    """エージェントのツール呼び出しを進捗イベント (stage, detail) に変換する"""
    if name == "search_accounts":
        query = str((args or {}).get("query") or "")
        return ("search", f"勘定科目を検索中{f' ({query})' if query else ''}")
    if name == "get_account_ledger":
        raw_codes = (args or {}).get("account_codes")
        if raw_codes is None:
            single = (args or {}).get("account_code")
            raw_codes = [single] if single else []
        if isinstance(raw_codes, str):
            raw_codes = [raw_codes]
        count = len([c for c in raw_codes if str(c).strip()])
        suffix = f" ({count}科目)" if count > 1 else ""
        return ("ledger", f"元帳を参照中{suffix}")
    if name == "get_account_list":
        return ("ledger", "元帳を参照中")
    if name == "search_journal_history":
        return ("history", "過去の仕訳を検索中")
    return ("thinking", "調査中")


def _select_default_account(user_id: int, preferred_names, type_codes):
    """決定的フォールバック用の既定科目を選ぶ。

    type_codes の優先順に種別を走査し、preferred_names に完全一致する科目 →
    部分一致 → 同種別の先頭科目、の順で 1 件返す。見つからなければ None。
    """
    for type_code in type_codes:
        atype = AccountType.query.filter_by(code=type_code).first()
        if atype is None:
            continue
        accounts = (
            Account.query
            .filter_by(user_id=user_id, is_active=True)
            .filter(Account.account_type_id == atype.id)
            .order_by(Account.code)
            .all()
        )
        if not accounts:
            continue
        for pref in preferred_names:
            for a in accounts:
                if a.name == pref:
                    return a
        for pref in preferred_names:
            for a in accounts:
                if pref in a.name or a.name in pref:
                    return a
        return accounts[0]
    return None


def _build_fallback_suggestion(user_id: int, date_str, description, amount):
    """抽出済みの金額と既定科目から仕訳案を機械的に組み立てる。

    金額が取れない、または費目・支払元科目が 1 つも見つからない場合は
    空リストを返す (仕訳として成立しないため)。
    """
    try:
        amount = int(amount or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return []

    expense = _select_default_account(
        user_id, _FALLBACK_EXPENSE_PREFERRED, ["expense"]
    )
    payment = _select_default_account(
        user_id, _FALLBACK_PAYMENT_PREFERRED, ["asset", "liability"]
    )
    if expense is None or payment is None:
        return []

    return [JournalSuggestion(
        title="自動計上（要確認）",
        description=(
            "AIが仕訳案を確定できなかったため、証憑から読み取った金額をもとに"
            "既定の勘定科目で自動生成しました。内容を確認してから登録してください。"
        ),
        date=date_str,
        entry_description=(description or "AI自動仕訳").strip(),
        lines=[
            {
                "account_code": expense.code,
                "account_name": expense.name,
                "debit_amount": amount,
                "credit_amount": 0,
            },
            {
                "account_code": payment.code,
                "account_name": payment.name,
                "debit_amount": 0,
                "credit_amount": amount,
            },
        ],
    )]


def _extract_document_basics(image_bytes, mime_type, comment, last_text,
                             api_key, provider, model, handler, extra_kw, user_id):
    """日付・摘要・金額を best-effort で抽出する。

    まずエージェントの最終応答テキストから JSON を拾い、金額が取れなければ
    ツールを使わない 1 回限りの抽出呼出しにフォールバックする。
    """
    info = {}
    if last_text:
        try:
            parsed = _extract_json(last_text)
            if isinstance(parsed, dict):
                info = parsed
        except Exception:
            info = {}

    def _amount_of(d):
        try:
            return int(d.get("amount") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0

    if _amount_of(info) > 0:
        return info

    prompt = DOCUMENT_PROMPT
    if comment:
        prompt += f"\n\nユーザーからのコメント: {comment}"
    try:
        parsed = _call_ai(
            handler, api_key, model, image_bytes, mime_type, prompt, 1000,
            user_id, extra_kw, provider=provider,
            feature="receipt_fallback_extract",
        )
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        logger.warning(
            "fallback document extraction failed for user %s", user_id,
            exc_info=True,
        )
    return info


def _deterministic_fallback_suggestion(user_id, image_bytes, mime_type, comment,
                                       api_key, provider, model, handler,
                                       extra_kw, last_text):
    """エージェントが確定できなかったときの最終手段 (無出力で落とさない)。"""
    from app.services.clock import today as _app_today
    info = _extract_document_basics(
        image_bytes, mime_type, comment, last_text,
        api_key, provider, model, handler, extra_kw, user_id,
    )
    if not info:
        return []
    date_str = info.get("date") or _app_today().isoformat()
    description = info.get("description") or comment or "AI自動仕訳"
    return _build_fallback_suggestion(
        user_id, date_str, description, info.get("amount")
    )


def _run_agent_loop(
    user_id: int,
    image_bytes: bytes,
    mime_type: str,
    comment: str = "",
    max_turns: int | None = None,
    progress_cb=None,
) -> list[JournalSuggestion]:
    """ツールコールを活用したAIエージェントループを実行する

    progress_cb が与えられた場合、(stage, detail) の進捗イベントを
    通知する。stage は "reading" / "thinking" / "search" / "ledger" /
    "history" / "composing" / "retry" のいずれか。コールバックの例外は
    解析本体に波及させない。

    max_turns が None の場合はユーザー設定 (UserAIConfig.max_turns) を使う。
    最終ターンでは submit_journal_suggestions を tool_choice で強制し、
    それでも確定できない場合は決定的フォールバックで最低 1 件の仕訳案を
    返す。無出力のままエラーにするのは、金額が読み取れず仕訳として
    成立しない場合に限る。
    """
    if max_turns is None:
        max_turns = _resolve_max_turns(user_id)
    else:
        max_turns = _clamp_max_turns(max_turns)

    def _emit(stage: str, detail: str) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(stage, detail)
        except Exception:
            logger.exception("progress callback failed (stage=%s)", stage)

    import time as _time
    api_key, provider, model, handler, custom_prompt, extra_kw = _get_ai_config(user_id)

    sys_prompt = AGENT_SYSTEM_PROMPT
    if custom_prompt:
        sys_prompt += f"\n\n## ユーザー定型ルール（必ず従ってください）\n{custom_prompt}"

    user_text = "添付の証憑画像を解析し、適切な勘定科目を検討して仕訳案を2〜4件提案してください。"
    if comment:
        user_text += f"\n\nユーザーからのコメント: {comment}"

    b64_img = base64.b64encode(image_bytes).decode()
    api_style = extra_kw.get("api_style", "chat_completions")
    is_responses_api = (provider == "openai_compatible" and api_style == "responses")

    if is_responses_api:
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"{sys_prompt}\n\n{user_text}"},
                    {
                        "type": "input_image",
                        "image_url": f"data:{mime_type};base64,{b64_img}",
                    },
                ],
            }
        ]
        step_fn = _agent_step_openai_responses
        append_results_fn = _append_tool_results_responses
    elif provider in ("openai", "llama_cpp", "openai_compatible"):
        history = [
            {"role": "system", "content": sys_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_img}"}},
                ],
            },
        ]
        step_fn = _agent_step_openai
        append_results_fn = _append_tool_results_openai
    elif provider == "anthropic":
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64_img}},
                    {"type": "text", "text": user_text},
                ],
            }
        ]
        step_fn = _agent_step_anthropic
        append_results_fn = _append_tool_results_anthropic
    elif provider == "google":
        history = [
            {
                "role": "user",
                "parts": [
                    {"text": user_text},
                    {"inline_data": {"mime_type": mime_type, "data": b64_img}},
                ],
            }
        ]
        step_fn = _agent_step_google
        append_results_fn = _append_tool_results_google
    else:
        raise ValueError(f"未対応のAIプロバイダーです: {provider}")

    last_text = ""
    _emit("reading", "証憑画像を読み取り中")
    for turn in range(max_turns):
        is_last_turn = (turn == max_turns - 1)
        _emit("thinking", f"仕訳を検討中 ({turn + 1}/{max_turns})")
        t0 = _time.perf_counter()
        try:
            step_kwargs = {"timeout": 60.0}
            if provider in ("anthropic", "google"):
                step_kwargs["system_prompt"] = sys_prompt
            elif provider in ("llama_cpp", "openai_compatible"):
                step_kwargs["base_url"] = extra_kw.get("base_url", "")
                step_kwargs["headers"] = extra_kw.get("headers", {})
            if is_last_turn:
                # 最終ターンは submit ツール呼び出しを強制し、確実に確定させる。
                step_kwargs["force_tool"] = "submit_journal_suggestions"

            text, tool_calls, history, usage = step_fn(
                api_key, model, history, AGENT_TOOLS, **step_kwargs
            )
            latency = int((_time.perf_counter() - t0) * 1000)
            _log_ai_usage(user_id, provider, model, f"agent_turn_{turn+1}", usage, latency)
            last_text = text
        except Exception as e:
            latency = int((_time.perf_counter() - t0) * 1000)
            status, http_status = _classify_exception(e)
            _log_ai_usage(user_id, provider, model, f"agent_turn_{turn+1}", {}, latency,
                          status=status, http_status=http_status)
            if is_last_turn and step_kwargs.get("force_tool"):
                # tool_choice 強制に未対応のプロバイダー等では最終ターンだけが
                # 失敗しうる。ここで落とさず決定的フォールバックへ回す。
                logger.warning(
                    "Forced final turn failed (turn %d) for user %s; "
                    "falling back to deterministic suggestion: %s",
                    turn + 1, user_id, e,
                )
                break
            logger.error("Agent step failed (turn %d) for user %s: %s", turn + 1, user_id, e)
            raise RuntimeError(f"AIエージェントの呼出しに失敗しました: {e}")

        # ツールコールの処理
        if tool_calls:
            results_to_append = []
            for tc in tool_calls:
                tc_id = tc["id"]
                tc_name = tc["name"]
                tc_args = tc.get("arguments") or {}

                # 確定ツールのチェック
                if tc_name == "submit_journal_suggestions":
                    raw_sugg = tc_args.get("suggestions", [])
                    suggestions = _parse_suggestions_list(user_id, raw_sugg)
                    if suggestions:
                        _emit("composing", "仕訳案をまとめ中")
                        return suggestions
                    # 無効な確定は検証エラーをLLMに返して再提出させる。
                    # _execute_agent_tool に落とすと「未知のツール」と嘘の
                    # 結果になり、LLMが修正できずターン枯渇→
                    # 「有効な仕訳案を生成できませんでした」に至る。
                    if isinstance(raw_sugg, list) and raw_sugg:
                        valid_codes = {
                            a.code for a in Account.query.filter_by(
                                user_id=user_id, is_active=True).all()
                        }
                        invalid = sorted({
                            str(line.get("account_code", ""))
                            for s in raw_sugg if isinstance(s, dict)
                            for line in s.get("lines", [])
                            if isinstance(line, dict)
                        } - valid_codes)
                        tool_output = {
                            "error": "仕訳案に無効な勘定科目コードが含まれています。"
                                     "有効なコードのみで再提出してください。",
                            "invalid_codes": invalid,
                        }
                    else:
                        tool_output = {
                            "error": "仕訳案が空です。2〜4件の仕訳案を"
                                     "submit_journal_suggestionsで再提出してください。",
                        }
                    if provider == "google":
                        results_to_append.append((tc_id, (tc_name, tool_output)))
                    else:
                        results_to_append.append((tc_id, tool_output))
                    _emit("retry", "仕訳案を修正中")
                    continue

                # 通常ツールの実行
                _emit(*_tool_progress(tc_name, tc_args))
                tool_output = _execute_agent_tool(user_id, tc_name, tc_args)
                if provider == "google":
                    results_to_append.append((tc_id, (tc_name, tool_output)))
                else:
                    results_to_append.append((tc_id, tool_output))

            history = append_results_fn(history, results_to_append)
        else:
            # ツールコールなし：テキストから仕訳案をフォールバック抽出
            suggestions = _fallback_extract_suggestions(last_text, user_id)
            if suggestions:
                _emit("composing", "仕訳案をまとめ中")
                return suggestions

            _emit("composing", "仕訳案の確定を要求中")

            prompt_msg = "仕訳案を確定するため、submit_journal_suggestions ツールを呼び出して2〜4件の仕訳案を提出してください。"
            if provider == "anthropic":
                history.append({"role": "user", "content": [{"type": "text", "text": prompt_msg}]})
            elif provider == "google":
                history.append({"role": "user", "parts": [{"text": prompt_msg}]})
            elif is_responses_api:
                history.append({"role": "user", "content": [{"type": "input_text", "text": prompt_msg}]})
            else:
                history.append({"role": "user", "content": prompt_msg})

    suggestions = _fallback_extract_suggestions(last_text, user_id)
    if suggestions:
        _emit("composing", "仕訳案をまとめ中")
        return suggestions

    # 決定的フォールバック: 日付・摘要・金額を抽出し、既定科目で機械的に
    # 1 件の仕訳案を組み立てる。これで「何も出さずに落ちる」経路を塞ぐ。
    _emit("composing", "代替の仕訳案を生成中")
    fallback = _deterministic_fallback_suggestion(
        user_id, image_bytes, mime_type, comment,
        api_key, provider, model, handler, extra_kw, last_text,
    )
    if fallback:
        return fallback

    raise RuntimeError(
        "AIが有効な仕訳案を生成できませんでした。"
        "画像を確認して再度お試しください。"
    )


def _legacy_analyze_and_suggest(user_id: int, image_bytes: bytes,
                                mime_type: str,
                                comment: str = "",
                                progress_cb=None) -> list[JournalSuggestion]:
    """レガシー2ラウンド方式（テスト互換用、progress_cb 対応）"""
    def _emit(stage: str, detail: str) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(stage, detail)
        except Exception:
            logger.exception("progress callback failed (stage=%s)", stage)

    api_key, provider, model, handler, custom_prompt, extra_kw = _get_ai_config(user_id)

    _emit("reading", "証憑画像を読み取り中")
    prompt = DOCUMENT_PROMPT
    if custom_prompt:
        prompt += f"\n\n## ユーザー定型情報\n{custom_prompt}"
    if comment:
        prompt += f"\n\nユーザーからのコメント: {comment}"
    round1 = _call_ai(handler, api_key, model, image_bytes, mime_type,
                      prompt, 1000, user_id, extra_kw,
                      provider=provider, feature="receipt_round1")

    analysis = DocumentAnalysis(
        date=round1.get("date"),
        description=round1.get("description", ""),
        amount=int(round1.get("amount", 0)),
        document_type=round1.get("document_type", "other"),
        items=round1.get("items", []),
        needs_ledger=round1.get("needs_ledger", False),
        requested_accounts=round1.get("requested_accounts", []),
    )

    ledger_text = ""
    if analysis.needs_ledger and analysis.requested_accounts:
        _emit("ledger", "元帳を参照中")
        ledger_text = _get_ledger_context(user_id, analysis.requested_accounts)

    account_list_text = _get_account_list_text(user_id)
    suggestion_prompt = _build_suggestion_prompt(
        account_list_text, ledger_text, custom_prompt
    )

    _emit("composing", "仕訳案を作成中")
    round2 = _call_ai(handler, api_key, model, image_bytes, mime_type,
                      suggestion_prompt, 2000, user_id, extra_kw,
                      provider=provider, feature="receipt_round2")

    suggestions_raw = round2.get("suggestions", [])
    valid_codes = {
        a.code for a in
        Account.query.filter_by(user_id=user_id, is_active=True).all()
    }

    suggestions = []
    for s in suggestions_raw:
        lines = []
        for line in s.get("lines", []):
            acode = str(line.get("account_code", ""))
            if acode in valid_codes:
                lines.append({
                    "account_code": acode,
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


def analyze_and_suggest(user_id: int, image_bytes: bytes,
                        mime_type: str,
                        comment: str = "",
                        progress_cb=None) -> list[JournalSuggestion]:
    """証憑画像を解析し、複数の仕訳案を生成する（ツールコール対応AIエージェント方式）

    AIエージェントが自律的に勘定科目検索・過去仕訳検索・元帳照会などのツールを呼び出し、
    最適な仕訳案（2〜4件）を提案します。
    progress_cb を渡すと (stage, detail) の進捗イベントを受け取れる。
    """
    from unittest.mock import Mock
    api_key, provider, model, handler, custom_prompt, extra_kw = _get_ai_config(user_id)
    if (
        isinstance(handler, Mock)
        or isinstance(_PROVIDER_HANDLERS.get(provider), Mock)
        or isinstance(_PROVIDER_HANDLERS.get("openai"), Mock)
    ):
        return _legacy_analyze_and_suggest(
            user_id, image_bytes, mime_type, comment=comment,
            progress_cb=progress_cb,
        )

    return _run_agent_loop(user_id, image_bytes, mime_type, comment=comment,
                           progress_cb=progress_cb)


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
    api_key, provider, model, _, __, extra_kw = _get_ai_config(user_id)

    text_handler = _TEXT_PROVIDER_HANDLERS.get(provider)
    if not text_handler:
        raise ValueError(f"未対応のAIプロバイダーです: {provider}")

    prompt = WEB_IMPORT_PROMPT.format(
        payment_account_name=payment_account_name,
        raw_text=raw_text[:50000],  # トークン上限を考慮して切り詰め
    )

    result = _call_ai_text(
        text_handler, api_key, model, prompt, 16000, user_id, extra_kw,
        provider=provider, feature="web_extract",
    )

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
    api_key, provider, model, _, __, extra_kw = _get_ai_config(user_id)
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


def analyze_voucher_for_attachment(
    user_id: int,
    image_bytes: bytes,
    mime_type: str,
    journal_date: str,
    journal_amount: int,
    journal_description: str,
) -> dict:
    """証憑添付用の軽量AI解析 — Round 1のみ + 整合性チェック。

    Returns:
        {"consistency": {...}}
    """
    api_key, provider, model, handler, _, extra_kw = _get_ai_config(user_id)

    prompt = DOCUMENT_PROMPT
    prompt += CONSISTENCY_CHECK_PROMPT.format(
        journal_date=journal_date,
        journal_amount=journal_amount,
        journal_description=journal_description,
    )

    result = _call_ai(
        handler, api_key, model, image_bytes, mime_type,
        prompt, 1500, user_id, extra_kw,
        provider=provider, feature="voucher_attach",
    )

    # 整合性チェック結果
    raw_con = result.get("consistency")
    if isinstance(raw_con, dict):
        con_status = raw_con.get("status", "warn")
        if con_status not in ("pass", "warn", "fail"):
            con_status = "warn"
        consistency_result = {
            "status": con_status,
            "date_match": bool(raw_con.get("date_match", False)),
            "amount_match": bool(raw_con.get("amount_match", False)),
            "description_match": bool(raw_con.get("description_match", False)),
            "warnings": raw_con.get("warnings", [])
            if isinstance(raw_con.get("warnings"), list) else [],
        }
    else:
        consistency_result = {
            "status": "warn",
            "date_match": False,
            "amount_match": False,
            "description_match": False,
            "warnings": ["AIが整合性チェック結果を返しませんでした"],
        }

    return {
        "consistency": consistency_result,
    }
