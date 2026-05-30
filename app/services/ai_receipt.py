"""AI証憑読取サービス - 証憑画像からの仕訳データ抽出

E2EE 化以降、サーバ側 LLM 呼出 (_call_ai / _PROVIDER_HANDLERS 等) は
すべてクライアント側 orchestrator (app/static/js/crypto/llm/) に移行済。
本モジュールは現在以下のみ提供する:
  - プロンプトテンプレ定数 (api.py の prompt-context endpoint が返却)
  - 元帳/科目一覧テキスト構築 helper (api.py が context 構築に使用)
  - AI usage log 記録 (`_log_ai_usage`)
  - dataclass (DocumentAnalysis / JournalSuggestion)
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import func

from app.extensions import db
from app.models.account import Account, AccountType
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


# Fernet 暗号化ヘルパー (_get_fernet / encrypt_api_key / decrypt_api_key) は
# E2EE 化完了に伴い削除。API キーは UserAIConfig.api_key_blob / api_key_iv に
# クライアント側で暗号化されて保存される (ai_config_api PUT 経由)。


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


# 旧 CONSISTENCY_CHECK_PROMPT (Python str.format 版) は
# analyze_voucher_for_attachment 廃止に伴い削除済。クライアント側
# voucher_attach_orchestrator.js が CONSISTENCY_CHECK_PROMPT_TEMPLATE
# (placeholder 版) を使用する。
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


# サーバ側 LLM 呼出経路 (_call_openai/_call_google/_call_anthropic/_call_llama_cpp
# とテキスト版、_usage_*/_extract_json/_PROVIDER_HANDLERS/_TEXT_PROVIDER_HANDLERS)
# は E2EE 化に伴い全 caller がクライアント完結に置き換わったため削除済。
# クライアント側 orchestrator (app/static/js/crypto/llm/) が等価の処理を実行。


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


# --- usage 記録 ---
#
# _get_ai_config (旧サーバ側 LLM 呼出のための AI 設定取得 + Fernet 復号 +
# entitlement check) は E2EE 化に伴い削除。client-side LLM 呼出では
# /api/v1/ai-config (E2EE blob 返却) + クライアント側 has_entitlement
# (presence_check のみ。entitlement gate は別途 API 層で実施) を使う。


# _log_ai_usage (旧サーバ側 _call_ai/_call_ai_text 内から呼ばれていた
# usage 記録 helper) は、サーバ側 LLM 呼出経路の削除に伴い caller がなくなり
# 削除。E2EE クライアント完結フローでは PATCH /api/v1/ai/drafts/<id>/suggestions
# 等のクライアント→サーバ通信で AIUsageLog を直接 INSERT する
# (app/views/api.py L847)。
#
# サーバ側 LLM 呼出ラッパー (_classify_exception / _call_ai / _call_ai_text) も
# 同様にクライアント完結フローに置き換わったため削除。クライアント側
# callLLM / callLLMText (app/static/js/crypto/llm/) が等価。


# 旧 AI_SUGGEST_CATEGORIES_PROMPT (Python str.format 版) は
# suggest_categories_by_ai 廃止に伴い削除済。クライアント側
# suggest_categories_orchestrator.js が AI_SUGGEST_CATEGORIES_PROMPT_TEMPLATE
# (placeholder 版) を使用する。
AI_SUGGEST_CATEGORIES_PROMPT_TEMPLATE = """あなたは日本の複式簿記の家計簿アプリのアシスタントです。
以下は取込先口座「__PAYMENT_ACCOUNT_NAME__」の元帳（過去の取引履歴）です。
各行の「相手科目」は、その取引で使われた費目（勘定科目）です。

__LEDGER_CONTEXT__

以下はユーザーの勘定科目一覧です。科目コードを使って回答してください。
__ACCOUNT_LIST__

以下の新規取引それぞれに、元帳のパターンを参考にして最も適切な勘定科目コードを推定してください。
- 出金は通常「費用」科目、入金は通常「収益」科目ですが、振替（資産・負債間の移動）の場合もあります。
- 元帳に似た摘要の取引がある場合は、その相手科目と同じ科目を優先してください。
- 該当なしの場合は account_code を null にしてください。

__ROWS_TEXT__

必ず以下のJSON形式のみを返してください。余計なテキストは不要です。
{"results": [{"index": 0, "account_code": "科目コード"}, ...]}"""


# _get_payment_ledger_context (支払口座の元帳テキスト構築) は E3-F PR-D-6-1a で
# 削除。平文 JournalEntry.date / description を読んでいたため、クライアント側
# (suggest_categories_orchestrator.js → crypto/ledger_context.js
# buildPaymentLedgerContext) が復号済み仕訳から等価のテキストを構築する。

# suggest_categories_by_ai は E2EE 化に伴い削除済。
# クライアント側 suggest_categories_orchestrator.js が等価の処理を実行。


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


# analyze_voucher_for_attachment は E2EE 化に伴い削除済。
# クライアント側 voucher_attach_orchestrator.js が等価の処理を実行する。
# CONSISTENCY_CHECK_PROMPT_TEMPLATE (placeholder 版) は
# /api/v1/voucher-attach/prompt-context から配信される。
