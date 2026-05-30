"""AI 解析サービス (services/ai_receipt.py) の残存 helper のテスト。

E2EE 化以降、サーバ側 LLM 呼出 (_call_ai / _PROVIDER_HANDLERS /
_get_ai_config / encrypt_api_key 等) はすべてクライアント完結 orchestrator
に置き換わったため、本ファイルは以下の helper のみテストする:

- match_account (suggest_categories の name → code 変換)
- _get_account_list_text (api.py の prompt-context endpoint が使用)
- _build_suggestion_prompt (api.py のプロンプト構築が使用)

E3-F PR-D-6-1a/1b: _get_payment_ledger_context / _get_ledger_context は平文読取の
ためクライアント側 (crypto/ledger_context.js buildPaymentLedgerContext /
buildAccountsLedgerContext) へ移植・削除した。そのテストは
tests/static/js/test_ledger_context.mjs でカバー済。

サーバ側 LLM 呼出の旧テスト (provider handlers / call_ai / get_ai_config) の
代替は tests/static/js/test_*_orchestrator.mjs (各 orchestrator 単体テスト) で
カバー済。
"""

from app.models.account import Account
from app.services.ai_receipt import (
    _build_suggestion_prompt,
    _get_account_list_text,
    match_account,
)


class TestMatchAccount:
    def test_exact_match(self, db, user, accounts):
        # 5010 = 食費
        result = match_account(user.id, "食費")
        assert result == "5010"

    def test_partial_match(self, db, user, accounts):
        # 「食費代」は「食費」を含むのでマッチ
        result = match_account(user.id, "食費代")
        assert result == "5010"

    def test_no_match_returns_first(self, db, user, accounts):
        # 全く関係ない名前 → 最初の費用科目
        result = match_account(user.id, "完全に未知")
        # accounts fixture には 5010, 5020 の費用科目がある
        assert result in ("5010", "5020")

    def test_no_expense_type(self, db, user, accounts):
        # 全 expense 科目を削除すると fallback で None
        Account.query.filter_by(user_id=user.id).filter(
            Account.code.in_(["5010", "5020"])
        ).delete()
        db.session.commit()
        result = match_account(user.id, "食費")
        assert result is None


class TestGetAccountListText:
    def test_returns_grouped_text(self, db, user, accounts):
        text = _get_account_list_text(user.id)
        assert "資産" in text
        assert "食費" in text


class TestBuildSuggestionPrompt:
    def test_empty(self):
        result = _build_suggestion_prompt("[科目一覧]", "", "")
        assert "[科目一覧]" in result

    def test_with_ledger(self):
        result = _build_suggestion_prompt("ACCT", "LEDGER", "")
        assert "LEDGER" in result

    def test_with_custom_prompt(self):
        result = _build_suggestion_prompt("ACCT", "", "CUSTOM")
        assert "CUSTOM" in result
