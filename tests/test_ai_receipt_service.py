"""AI 解析サービス (services/ai_receipt.py) のテスト

外部 API への httpx 呼出しはモック化。プロバイダー別ハンドラー、
JSON抽出、設定取得、メイン関数 (suggest_categories_by_ai / match_account /
analyze_voucher_for_attachment) をカバー。

E2 PR-C-4i: analyze_receipt / analyze_and_suggest / parse_web_text 関連は
caller を全廃済のため削除。残った機能のみテスト。
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.models.account import Account
from app.models.ai_config import UserAIConfig
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.ai_receipt import (
    DocumentAnalysis,
    JournalSuggestion,
    PROVIDER_DEFAULTS,
    _build_suggestion_prompt,
    _call_ai,
    _call_anthropic,
    _call_anthropic_text,
    _call_google,
    _call_google_text,
    _call_llama_cpp,
    _call_llama_cpp_text,
    _call_openai,
    _call_openai_text,
    _extract_json,
    _get_account_list_text,
    _get_ai_config,
    _get_ledger_context,
    _get_payment_ledger_context,
    decrypt_api_key,
    encrypt_api_key,
    match_account,
)


def _h(parsed):
    """v3.13.0 以降のハンドラ戻り値 (parsed, usage) をテスト用に組み立てる。

    既存テストは parsed のみ気にしているので usage は空辞書で十分。
    """
    return (parsed, {"input_tokens": None, "output_tokens": None})


def _ai_config(db, user_id, provider="openai"):
    cfg = UserAIConfig(
        user_id=user_id, provider=provider,
        api_key_encrypted=encrypt_api_key("test-key"),
        model_name="gpt-4o",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _llama_cpp_config(db, user_id):
    cfg = UserAIConfig(
        user_id=user_id, provider="llama_cpp",
        api_key_encrypted=encrypt_api_key("_"),  # llama.cpp はダミーキー
        model_name="default",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _mock_post(json_response, status=200):
    """httpx.post のモックヘルパー"""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_response
    resp.status_code = status
    resp.raise_for_status.return_value = None
    return resp


class TestEncryptDecrypt:
    def test_roundtrip(self, app):
        with app.app_context():
            enc = encrypt_api_key("sk-secret")
            dec = decrypt_api_key(enc)
            assert dec == "sk-secret"

    def test_decrypt_invalid(self, app):
        with app.app_context():
            with pytest.raises(ValueError):
                decrypt_api_key(b"not-encrypted")


class TestExtractJson:
    def test_plain_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_json_in_markdown(self):
        text = '```json\n{"a": 2}\n```'
        assert _extract_json(text) == {"a": 2}

    def test_json_in_markdown_no_lang(self):
        text = '```\n{"a": 3}\n```'
        assert _extract_json(text) == {"a": 3}

    def test_json_in_text(self):
        text = "前置き {\"a\": 4} 後置き"
        assert _extract_json(text) == {"a": 4}

    def test_no_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _extract_json("just text, no json")


class TestProviderHandlers:
    """v3.13.0 以降、ハンドラは (parsed_json, usage_dict) のタプルを返す。"""

    def test_openai(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"date": "2026-02-15", "amount": 100}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 34},
            })
            result, usage = _call_openai("k", "gpt-4o", b"img", "image/jpeg")
            assert result["amount"] == 100
            assert usage == {"input_tokens": 12, "output_tokens": 34}
            sent = mock_post.call_args.kwargs["json"]
            assert "messages" in sent
            assert "data:image/jpeg;base64," in sent["messages"][0]["content"][1]["image_url"]["url"]

    def test_google(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "candidates": [{"content": {"parts": [{"text": '{"date": null, "amount": 50}'}]}}],
                "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 22},
            })
            result, usage = _call_google("k", "gemini", b"img", "image/png")
            assert result["amount"] == 50
            assert usage == {"input_tokens": 11, "output_tokens": 22}

    def test_anthropic(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "content": [{"text": '{"date": "2026-01-01", "amount": 200}'}],
                "usage": {"input_tokens": 13, "output_tokens": 26},
            })
            result, usage = _call_anthropic("k", "claude", b"img", "image/jpeg")
            assert result["amount"] == 200
            assert usage == {"input_tokens": 13, "output_tokens": 26}

    def test_llama_cpp(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"amount": 30}'}}],
            })
            result, usage = _call_llama_cpp("", "default", b"img", "image/png",
                                            base_url="http://x:8080")
            assert result["amount"] == 30
            # usage 欠落時は None
            assert usage == {"input_tokens": None, "output_tokens": None}
            url = mock_post.call_args.args[0]
            assert "x:8080" in url

    def test_llama_cpp_default_url(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"amount": 0}'}}],
            })
            _call_llama_cpp("", "default", b"img", "image/png")
            url = mock_post.call_args.args[0]
            assert "localhost:8080" in url


class TestTextHandlers:
    def test_openai_text(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"results": []}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            })
            result, usage = _call_openai_text("k", "gpt-4", "prompt")
            assert result == {"results": []}
            assert usage == {"input_tokens": 5, "output_tokens": 7}

    def test_google_text(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "candidates": [{"content": {"parts": [{"text": '{"x": 1}'}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
            })
            result, usage = _call_google_text("k", "g", "p")
            assert result == {"x": 1}
            assert usage == {"input_tokens": 3, "output_tokens": 4}

    def test_anthropic_text(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "content": [{"text": '{"x": 2}'}],
                "usage": {"input_tokens": 8, "output_tokens": 9},
            })
            result, usage = _call_anthropic_text("k", "c", "p")
            assert result == {"x": 2}
            assert usage == {"input_tokens": 8, "output_tokens": 9}

    def test_llama_cpp_text(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"x": 3}'}}],
            })
            result, usage = _call_llama_cpp_text("", "default", "p", base_url="http://x:8080")
            assert result == {"x": 3}
            assert usage == {"input_tokens": None, "output_tokens": None}


class TestGetAiConfig:
    def test_no_config_raises(self, db, user, accounts):
        with pytest.raises(ValueError):
            _get_ai_config(user.id)

    def test_openai_config(self, db, user, accounts):
        _ai_config(db, user.id)
        api_key, provider, model, handler, custom, extra, compliance = _get_ai_config(user.id)
        assert provider == "openai"
        assert handler is _call_openai
        assert extra == {}

    def test_llama_cpp_config_uses_server_url(self, db, user, accounts):
        """サーバー設定 LLAMA_CPP_URL が base_url として注入される。"""
        _llama_cpp_config(db, user.id)
        api_key, provider, _, _, _, extra, _ = _get_ai_config(user.id)
        assert api_key == ""  # ダミー "_" が空文字に
        assert provider == "llama_cpp"
        # TestConfig で http://test-llama-cpp:8080 が設定されている
        assert extra == {"base_url": "http://test-llama-cpp:8080"}

    def test_llama_cpp_without_server_url_raises(self, db, user, accounts, app):
        """サーバー管理者が提供を停止した場合 (LLAMA_CPP_URL 未設定) は
        「設定を変更してください」と案内するエラーになる。"""
        _llama_cpp_config(db, user.id)
        app.config["LLAMA_CPP_URL"] = ""
        try:
            with pytest.raises(ValueError, match="サーバー管理者"):
                _get_ai_config(user.id)
        finally:
            app.config["LLAMA_CPP_URL"] = "http://test-llama-cpp:8080"

    def test_legacy_ollama_provider_unsupported(self, db, user, accounts):
        """v3.12.0 で ollama サポート削除。既存設定は未対応エラーになる。"""
        cfg = UserAIConfig(
            user_id=user.id, provider="ollama",
            api_key_encrypted=encrypt_api_key("_"),
            model_name="llama3",
        )
        db.session.add(cfg)
        db.session.commit()
        with pytest.raises(ValueError, match="未対応"):
            _get_ai_config(user.id)

    def test_unknown_provider(self, db, user, accounts):
        # 強制的に未対応プロバイダにする
        cfg = UserAIConfig(
            user_id=user.id, provider="unknown",
            api_key_encrypted=encrypt_api_key("k"),
            model_name="x",
        )
        db.session.add(cfg)
        db.session.commit()
        with pytest.raises(ValueError):
            _get_ai_config(user.id)

    def test_default_model(self, db, user, accounts):
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_encrypted=encrypt_api_key("k"),
            model_name="",  # 空ならデフォルト
        )
        db.session.add(cfg)
        db.session.commit()
        _, _, model, _, _, _, _ = _get_ai_config(user.id)
        assert model == PROVIDER_DEFAULTS["openai"]


class TestCallAi:
    def test_http_status_error(self, db, user, accounts):
        handler = MagicMock()
        request = MagicMock()
        response = MagicMock(status_code=500)
        handler.side_effect = httpx.HTTPStatusError(
            "500", request=request, response=response,
        )
        with pytest.raises(RuntimeError) as exc:
            _call_ai(handler, "k", "m", b"x", "image/png", "p", 100, user.id)
        assert "HTTP 500" in str(exc.value)

    def test_general_error(self, db, user, accounts):
        handler = MagicMock()
        handler.side_effect = ValueError("bad")
        with pytest.raises(RuntimeError):
            _call_ai(handler, "k", "m", b"x", "image/png", "p", 100, user.id)


# E2 PR-C-4i: TestAnalyzeReceipt / TestAnalyzeAndSuggest / TestParseWebText は
# 対応関数の削除に伴い削除。元帳取得 / プロンプト構築 / バリデーション等の
# E2EE クライアント完結フロー側の動作は tests/test_ai_uploads_api.py +
# tests/static/js/test_ai_journal_orchestrator.mjs で担保されている。


# E2 PR-C-6b: TestSuggestCategoriesByAi は対応関数の削除に伴い削除。
# 等価のクライアント側ロジック (rows_text 構築 / プロンプト組立 / index
# 検証 / account_map マッピング / HTTP エラー伝播) は
# tests/static/js/test_suggest_categories_orchestrator.mjs でカバー。


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
        # 費用科目が無いユーザーで呼ぶ
        from app.models.account import AccountType
        # 全 expense 科目を削除 (テスト用に AccountType を差し替えると壊れるので skip)
        Account.query.filter_by(user_id=user.id).filter(
            Account.code.in_(["5010", "5020"])
        ).delete()
        db.session.commit()
        result = match_account(user.id, "食費")
        # 費用科目なしでも fallback で None
        assert result is None


class TestGetLedgerContext:
    def test_no_match(self, db, user, accounts):
        result = _get_ledger_context(user.id, ["完全に存在しない"])
        assert result == ""

    def test_with_entries(self, db, user, accounts):
        from tests.conftest import make_journal
        make_journal(db, user.id, "5010", "1010", 1000)
        result = _get_ledger_context(user.id, ["食費"])
        assert "食費" in result


class TestGetAccountListText:
    def test_returns_grouped_text(self, db, user, accounts):
        text = _get_account_list_text(user.id)
        assert "資産" in text
        assert "食費" in text


class TestGetPaymentLedgerContext:
    def test_unknown_account(self, db, user, accounts):
        result = _get_payment_ledger_context(user.id, "9999")
        assert result == ""

    def test_no_entries(self, db, user, accounts):
        result = _get_payment_ledger_context(user.id, "1010")
        assert "元帳データなし" in result

    def test_with_entries(self, db, user, accounts):
        from tests.conftest import make_journal
        make_journal(db, user.id, "5010", "1010", 1500)
        result = _get_payment_ledger_context(user.id, "1010")
        assert "現金" in result


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


# E2 PR-C-6a: TestAnalyzeVoucherForAttachment は対応関数の削除に伴い削除。
# 同等のクライアント側ロジック (compliance/consistency 整形) は
# tests/static/js/test_voucher_attach_orchestrator.mjs でカバー。
