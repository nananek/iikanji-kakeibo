"""AI 解析サービス (services/ai_receipt.py) のテスト

外部 API への httpx 呼出しはモック化。プロバイダー別ハンドラー、
JSON抽出、設定取得、メイン関数 (analyze_receipt / analyze_and_suggest /
parse_web_text / suggest_categories_by_ai / match_account /
analyze_voucher_for_attachment) をカバー。
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
    ReceiptData,
    _build_suggestion_prompt,
    _call_ai,
    _call_anthropic,
    _call_anthropic_text,
    _call_google,
    _call_google_text,
    _call_ollama,
    _call_ollama_text,
    _call_openai,
    _call_openai_text,
    _extract_json,
    _get_account_list_text,
    _get_ai_config,
    _get_ledger_context,
    _get_payment_ledger_context,
    analyze_receipt,
    analyze_and_suggest,
    analyze_voucher_for_attachment,
    decrypt_api_key,
    encrypt_api_key,
    match_account,
    parse_web_text,
    suggest_categories_by_ai,
)


def _ai_config(db, user_id, provider="openai"):
    cfg = UserAIConfig(
        user_id=user_id, provider=provider,
        api_key_encrypted=encrypt_api_key("test-key"),
        model_name="gpt-4o",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _ollama_config(db, user_id):
    cfg = UserAIConfig(
        user_id=user_id, provider="ollama",
        api_key_encrypted=encrypt_api_key("_"),  # Ollama はダミーキー
        model_name="llama3.2-vision",
        base_url="http://localhost:11434",
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
    def test_openai(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"date": "2026-02-15", "amount": 100}'}}],
            })
            result = _call_openai("k", "gpt-4o", b"img", "image/jpeg")
            assert result["amount"] == 100
            # base64 でエンコードされた image が渡る
            sent = mock_post.call_args.kwargs["json"]
            assert "messages" in sent
            assert "data:image/jpeg;base64," in sent["messages"][0]["content"][1]["image_url"]["url"]

    def test_google(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "candidates": [{"content": {"parts": [{"text": '{"date": null, "amount": 50}'}]}}],
            })
            result = _call_google("k", "gemini", b"img", "image/png")
            assert result["amount"] == 50

    def test_anthropic(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "content": [{"text": '{"date": "2026-01-01", "amount": 200}'}],
            })
            result = _call_anthropic("k", "claude", b"img", "image/jpeg")
            assert result["amount"] == 200

    def test_ollama(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"amount": 30}'}}],
            })
            result = _call_ollama("", "llama", b"img", "image/png",
                                    base_url="http://x:11434")
            assert result["amount"] == 30
            url = mock_post.call_args.args[0]
            assert "x:11434" in url

    def test_ollama_default_url(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"amount": 0}'}}],
            })
            _call_ollama("", "llama", b"img", "image/png")
            url = mock_post.call_args.args[0]
            assert "localhost:11434" in url


class TestTextHandlers:
    def test_openai_text(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"results": []}'}}],
            })
            result = _call_openai_text("k", "gpt-4", "prompt")
            assert result == {"results": []}

    def test_google_text(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "candidates": [{"content": {"parts": [{"text": '{"x": 1}'}]}}],
            })
            assert _call_google_text("k", "g", "p") == {"x": 1}

    def test_anthropic_text(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "content": [{"text": '{"x": 2}'}],
            })
            assert _call_anthropic_text("k", "c", "p") == {"x": 2}

    def test_ollama_text(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"x": 3}'}}],
            })
            assert _call_ollama_text("", "l", "p", base_url="http://x:11434") == {"x": 3}


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

    def test_ollama_config_strips_dummy_key(self, db, user, accounts):
        _ollama_config(db, user.id)
        api_key, provider, _, _, _, extra, _ = _get_ai_config(user.id)
        assert api_key == ""  # ダミー "_" が空文字に
        assert provider == "ollama"
        assert extra == {"base_url": "http://localhost:11434"}

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


class TestAnalyzeReceipt:
    def test_success(self, db, user, accounts):
        _ai_config(db, user.id)
        mock_call = MagicMock(); _patches_openai = patch.dict("app.services.ai_receipt._PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_openai:
            mock_call.return_value = {
                "date": "2026-02-15",
                "description": "セブン",
                "amount": 500,
                "category": "食費",
            }
            r = analyze_receipt(user.id, b"img", "image/jpeg")
            assert isinstance(r, ReceiptData)
            assert r.amount == 500
            assert r.suggested_category == "食費"


class TestAnalyzeAndSuggest:
    def test_success_simple(self, db, user, accounts):
        _ai_config(db, user.id)
        mock_call = MagicMock(); _patches_openai = patch.dict("app.services.ai_receipt._PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_openai:
            # 第1ラウンドと第2ラウンドの結果
            mock_call.side_effect = [
                {  # round 1
                    "date": "2026-02-15", "description": "セブン",
                    "amount": 500, "document_type": "receipt",
                    "items": [], "needs_ledger": False, "requested_accounts": [],
                },
                {  # round 2
                    "suggestions": [
                        {
                            "title": "食費として計上",
                            "description": "...",
                            "date": "2026-02-15",
                            "entry_description": "セブン",
                            "lines": [
                                {"account_code": "5010", "account_name": "食費",
                                 "debit_amount": 500, "credit_amount": 0},
                                {"account_code": "1010", "account_name": "現金",
                                 "debit_amount": 0, "credit_amount": 500},
                            ],
                        },
                    ],
                },
            ]
            results = analyze_and_suggest(user.id, b"img", "image/jpeg")
            assert len(results) == 1
            assert results[0].title == "食費として計上"

    def test_invalid_account_codes_filtered(self, db, user, accounts):
        _ai_config(db, user.id)
        mock_call = MagicMock(); _patches_openai = patch.dict("app.services.ai_receipt._PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_openai:
            mock_call.side_effect = [
                {"document_type": "receipt", "items": [],
                 "needs_ledger": False, "requested_accounts": [],
                 "amount": 100, "description": "x", "date": "2026-02-15"},
                {"suggestions": [{
                    "title": "x", "description": "y",
                    "date": "2026-02-15", "entry_description": "x",
                    "lines": [
                        {"account_code": "9999", "debit_amount": 100,
                         "credit_amount": 0},  # 存在しない
                        {"account_code": "8888", "debit_amount": 0,
                         "credit_amount": 100},  # 存在しない
                    ],
                }]},
            ]
            with pytest.raises(RuntimeError):
                analyze_and_suggest(user.id, b"img", "image/jpeg")

    def test_ledger_fetch_when_needed(self, db, user, accounts):
        _ai_config(db, user.id)
        mock_call = MagicMock()
        with patch.dict("app.services.ai_receipt._PROVIDER_HANDLERS", {"openai": mock_call}), \
             patch("app.services.ai_receipt._get_ledger_context") as mock_ledger:
            mock_ledger.return_value = "(元帳サンプル)"
            mock_call.side_effect = [
                {"document_type": "payslip", "items": [],
                 "needs_ledger": True, "requested_accounts": ["給料手当"],
                 "amount": 250000, "description": "給与", "date": "2026-02-25"},
                {"suggestions": [{
                    "title": "x", "description": "y",
                    "date": "2026-02-25", "entry_description": "給与",
                    "lines": [
                        {"account_code": "5010", "debit_amount": 250000,
                         "credit_amount": 0},
                        {"account_code": "1010", "debit_amount": 0,
                         "credit_amount": 250000},
                    ],
                }]},
            ]
            analyze_and_suggest(user.id, b"img", "image/jpeg")
            mock_ledger.assert_called_once()

    def test_with_compliance_check(self, db, user, accounts):
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_encrypted=encrypt_api_key("k"),
            model_name="gpt-4o",
            compliance_check=True,
        )
        db.session.add(cfg)
        db.session.commit()

        mock_call = MagicMock(); _patches_openai = patch.dict("app.services.ai_receipt._PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_openai:
            mock_call.side_effect = [
                {"document_type": "receipt", "items": [],
                 "needs_ledger": False, "requested_accounts": [],
                 "amount": 100, "description": "x", "date": "2026-02-15",
                 "compliance": {
                     "status": "warn",
                     "warnings": ["切れ"],
                     "details": ["影あり"],
                 }},
                {"suggestions": [{
                    "title": "x", "description": "y",
                    "date": "2026-02-15", "entry_description": "x",
                    "lines": [
                        {"account_code": "5010", "debit_amount": 100,
                         "credit_amount": 0},
                        {"account_code": "1010", "debit_amount": 0,
                         "credit_amount": 100},
                    ],
                }]},
            ]
            results = analyze_and_suggest(user.id, b"img", "image/jpeg")
            assert results[0].compliance is not None
            assert results[0].compliance["status"] == "warn"


class TestParseWebText:
    def test_unknown_provider(self, db, user, accounts):
        cfg = UserAIConfig(
            user_id=user.id, provider="bad",
            api_key_encrypted=encrypt_api_key("k"),
            model_name="x",
        )
        db.session.add(cfg)
        db.session.commit()
        with pytest.raises(ValueError):
            parse_web_text(user.id, "txt", "口座")

    def test_success(self, db, user, accounts):
        _ai_config(db, user.id)
        mock_call = MagicMock(); _patches_text = patch.dict("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_text:
            mock_call.return_value = {
                "transactions": [
                    {"date": "2026-02-15", "description": "ATM",
                     "deposit": 0, "withdrawal": 5000},
                    {"date": "2026-02-16", "description": "給与",
                     "deposit": 250000, "withdrawal": 0},
                ],
            }
            result = parse_web_text(user.id, "明細テキスト", "三井住友")
            assert len(result) == 2
            assert result[0]["row_num"] == 1
            assert result[0]["withdrawal"] == 5000

    def test_http_error(self, db, user, accounts):
        _ai_config(db, user.id)
        mock_call = MagicMock(); _patches_text = patch.dict("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_text:
            request = MagicMock()
            response = MagicMock(status_code=500)
            mock_call.side_effect = httpx.HTTPStatusError(
                "500", request=request, response=response,
            )
            with pytest.raises(RuntimeError):
                parse_web_text(user.id, "x", "y")

    def test_general_error(self, db, user, accounts):
        _ai_config(db, user.id)
        mock_call = MagicMock(); _patches_text = patch.dict("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_text:
            mock_call.side_effect = ValueError("bad")
            with pytest.raises(RuntimeError):
                parse_web_text(user.id, "x", "y")


class TestSuggestCategoriesByAi:
    def test_unknown_provider(self, db, user, accounts):
        cfg = UserAIConfig(
            user_id=user.id, provider="bad",
            api_key_encrypted=encrypt_api_key("k"),
            model_name="x",
        )
        db.session.add(cfg)
        db.session.commit()
        with pytest.raises(ValueError):
            suggest_categories_by_ai(user.id, "1010", [])

    def test_success(self, db, user, accounts):
        _ai_config(db, user.id)
        rows = [{"description": "セブン", "deposit": 0, "withdrawal": 500}]
        mock_call = MagicMock(); _patches_text = patch.dict("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_text:
            mock_call.return_value = {
                "results": [{"index": 0, "account_code": "5010"}],
            }
            result = suggest_categories_by_ai(user.id, "1010", rows)
            assert "セブン" in result
            assert result["セブン"]["account_code"] == "5010"

    def test_invalid_index_skipped(self, db, user, accounts):
        _ai_config(db, user.id)
        rows = [{"description": "x", "deposit": 0, "withdrawal": 100}]
        mock_call = MagicMock(); _patches_text = patch.dict("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_text:
            mock_call.return_value = {
                "results": [
                    {"index": 99, "account_code": "5010"},  # 範囲外
                    {"index": None, "account_code": "5010"},
                    {"index": 0, "account_code": None},
                ],
            }
            result = suggest_categories_by_ai(user.id, "1010", rows)
            assert result == {}

    def test_inactive_account_not_in_output(self, db, user, accounts):
        _ai_config(db, user.id)
        rows = [{"description": "x", "deposit": 0, "withdrawal": 100}]
        mock_call = MagicMock(); _patches_text = patch.dict("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_text:
            mock_call.return_value = {
                "results": [{"index": 0, "account_code": "9999"}],  # 存在しない
            }
            result = suggest_categories_by_ai(user.id, "1010", rows)
            assert result == {}

    def test_http_error(self, db, user, accounts):
        _ai_config(db, user.id)
        mock_call = MagicMock(); _patches_text = patch.dict("app.services.ai_receipt._TEXT_PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_text:
            request = MagicMock()
            response = MagicMock(status_code=500)
            mock_call.side_effect = httpx.HTTPStatusError(
                "500", request=request, response=response,
            )
            with pytest.raises(RuntimeError):
                suggest_categories_by_ai(user.id, "1010", [{"description": "x"}])


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


class TestAnalyzeVoucherForAttachment:
    def test_success(self, db, user, accounts):
        _ai_config(db, user.id)
        mock_call = MagicMock(); _patches_openai = patch.dict("app.services.ai_receipt._PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_openai:
            mock_call.return_value = {
                "date": "2026-02-15", "description": "x",
                "amount": 100, "document_type": "receipt",
                "consistency": {
                    "status": "pass",
                    "date_match": True,
                    "amount_match": True,
                    "description_match": True,
                    "warnings": [],
                },
            }
            result = analyze_voucher_for_attachment(
                user.id, b"img", "image/jpeg",
                journal_date="2026-02-15", journal_amount=100,
                journal_description="x",
            )
            assert result["consistency"]["status"] == "pass"

    def test_consistency_missing_defaults(self, db, user, accounts):
        _ai_config(db, user.id)
        mock_call = MagicMock(); _patches_openai = patch.dict("app.services.ai_receipt._PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_openai:
            mock_call.return_value = {
                "date": "2026-02-15", "description": "x",
                "amount": 100, "document_type": "receipt",
                # consistency missing
            }
            result = analyze_voucher_for_attachment(
                user.id, b"img", "image/jpeg",
                journal_date="2026-02-15", journal_amount=100,
                journal_description="x",
            )
            assert result["consistency"]["status"] == "warn"
            assert result["consistency"]["date_match"] is False

    def test_with_compliance_check(self, db, user, accounts):
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_encrypted=encrypt_api_key("k"),
            model_name="gpt-4o", compliance_check=True,
        )
        db.session.add(cfg)
        db.session.commit()
        mock_call = MagicMock(); _patches_openai = patch.dict("app.services.ai_receipt._PROVIDER_HANDLERS", {"openai": mock_call})
        with _patches_openai:
            mock_call.return_value = {
                "date": "2026-02-15", "description": "x",
                "amount": 100, "document_type": "receipt",
                "compliance": {
                    "status": "pass", "warnings": [], "details": [],
                },
                "consistency": {
                    "status": "pass",
                    "date_match": True, "amount_match": True,
                    "description_match": True, "warnings": [],
                },
            }
            result = analyze_voucher_for_attachment(
                user.id, b"img", "image/jpeg",
                journal_date="2026-02-15", journal_amount=100,
                journal_description="x",
            )
            assert result["compliance"]["status"] == "pass"
