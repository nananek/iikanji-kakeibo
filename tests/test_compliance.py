"""電帳法コンプライアンスチェック（Phase 1.5）テスト"""

import json
from dataclasses import asdict
from unittest.mock import patch, MagicMock

import pytest

from app.extensions import db
from app.models.ai_config import UserAIConfig
from app.services.ai_receipt import (
    JournalSuggestion,
    COMPLIANCE_CHECK_PROMPT,
    DOCUMENT_PROMPT,
)


class TestJournalSuggestionCompliance:
    """JournalSuggestion dataclass の compliance フィールド"""

    def test_default_compliance_is_none(self):
        s = JournalSuggestion(
            title="t", description="d", date="2025-01-01",
            entry_description="e", lines=[],
        )
        assert s.compliance is None

    def test_compliance_with_pass(self):
        s = JournalSuggestion(
            title="t", description="d", date="2025-01-01",
            entry_description="e", lines=[],
            compliance={"status": "pass", "warnings": []},
        )
        assert s.compliance["status"] == "pass"
        assert s.compliance["warnings"] == []

    def test_compliance_serializes_via_asdict(self):
        s = JournalSuggestion(
            title="t", description="d", date="2025-01-01",
            entry_description="e", lines=[],
            compliance={"status": "warn", "warnings": ["ぼやけ"]},
        )
        d = asdict(s)
        assert d["compliance"]["status"] == "warn"
        assert "ぼやけ" in d["compliance"]["warnings"]

    def test_none_compliance_serializes_as_none(self):
        s = JournalSuggestion(
            title="t", description="d", date="2025-01-01",
            entry_description="e", lines=[],
        )
        d = asdict(s)
        assert d["compliance"] is None


class TestComplianceCheckConfig:
    """UserAIConfig.compliance_check のテスト"""

    def test_default_is_false(self, db, user):
        from app.services.ai_receipt import encrypt_api_key
        config = UserAIConfig(
            user_id=user.id,
            provider="openai",
            api_key_encrypted=encrypt_api_key("sk-test"),
        )
        db.session.add(config)
        db.session.commit()
        assert config.compliance_check is False

    def test_can_enable(self, db, user):
        from app.services.ai_receipt import encrypt_api_key
        config = UserAIConfig(
            user_id=user.id,
            provider="openai",
            api_key_encrypted=encrypt_api_key("sk-test"),
            compliance_check=True,
        )
        db.session.add(config)
        db.session.commit()
        loaded = UserAIConfig.query.filter_by(user_id=user.id).first()
        assert loaded.compliance_check is True


class TestCompliancePromptInjection:
    """analyze_and_suggest でコンプライアンスプロンプトが注入されること"""

    def _setup_config(self, db_sess, user_id, compliance_check=False):
        from app.services.ai_receipt import encrypt_api_key
        config = UserAIConfig(
            user_id=user_id,
            provider="openai",
            api_key_encrypted=encrypt_api_key("sk-test"),
            model_name="gpt-4o",
            compliance_check=compliance_check,
        )
        db_sess.session.add(config)
        db_sess.session.commit()

    @patch("app.services.ai_receipt._call_ai")
    def test_compliance_disabled_no_prompt(self, mock_call, db, user, accounts):
        """compliance_check=False のとき COMPLIANCE_CHECK_PROMPT は含まれない"""
        self._setup_config(db, user.id, compliance_check=False)

        # Round 1 response
        mock_call.side_effect = [
            {
                "date": "2025-01-15", "description": "テスト",
                "amount": 1000, "document_type": "receipt",
                "items": [], "needs_ledger": False,
                "requested_accounts": [],
            },
            {
                "suggestions": [{
                    "title": "費用計上",
                    "description": "desc",
                    "date": "2025-01-15",
                    "entry_description": "テスト",
                    "lines": [
                        {"account_code": accounts["5010"].code, "account_name": "消耗品費",
                         "debit_amount": 1000, "credit_amount": 0},
                        {"account_code": accounts["1010"].code, "account_name": "現金",
                         "debit_amount": 0, "credit_amount": 1000},
                    ],
                }],
            },
        ]

        from app.services.ai_receipt import analyze_and_suggest
        suggestions = analyze_and_suggest(user.id, b"fake", "image/jpeg")

        # Round 1 のプロンプトを確認 (_call_ai の第6引数 = index 5)
        r1_prompt = mock_call.call_args_list[0][0][5]
        assert "電帳法コンプライアンスチェック" not in r1_prompt
        assert suggestions[0].compliance is None

    @patch("app.services.ai_receipt._call_ai")
    def test_compliance_enabled_injects_prompt(self, mock_call, db, user, accounts):
        """compliance_check=True のとき COMPLIANCE_CHECK_PROMPT が含まれる"""
        self._setup_config(db, user.id, compliance_check=True)

        mock_call.side_effect = [
            {
                "date": "2025-01-15", "description": "テスト",
                "amount": 1000, "document_type": "receipt",
                "items": [], "needs_ledger": False,
                "requested_accounts": [],
                "compliance": {
                    "status": "warn",
                    "warnings": ["やや影がかかっています"],
                },
            },
            {
                "suggestions": [{
                    "title": "費用計上",
                    "description": "desc",
                    "date": "2025-01-15",
                    "entry_description": "テスト",
                    "lines": [
                        {"account_code": accounts["5010"].code, "account_name": "消耗品費",
                         "debit_amount": 1000, "credit_amount": 0},
                        {"account_code": accounts["1010"].code, "account_name": "現金",
                         "debit_amount": 0, "credit_amount": 1000},
                    ],
                }],
            },
        ]

        from app.services.ai_receipt import analyze_and_suggest
        suggestions = analyze_and_suggest(user.id, b"fake", "image/jpeg")

        # Round 1 のプロンプトを確認 (_call_ai の第6引数 = index 5)
        r1_prompt = mock_call.call_args_list[0][0][5]
        assert "電帳法コンプライアンスチェック" in r1_prompt

        assert suggestions[0].compliance is not None
        assert suggestions[0].compliance["status"] == "warn"
        assert "やや影がかかっています" in suggestions[0].compliance["warnings"]

    @patch("app.services.ai_receipt._call_ai")
    def test_compliance_pass_result(self, mock_call, db, user, accounts):
        """AI が compliance pass を返すケース"""
        self._setup_config(db, user.id, compliance_check=True)

        mock_call.side_effect = [
            {
                "date": "2025-01-15", "description": "テスト",
                "amount": 1000, "document_type": "receipt",
                "items": [], "needs_ledger": False,
                "requested_accounts": [],
                "compliance": {
                    "status": "pass", "warnings": [],
                    "details": ["画像品質: 鮮明", "必須情報: 確認済み", "書類妥当性: 有効"],
                },
            },
            {
                "suggestions": [{
                    "title": "費用計上",
                    "description": "desc",
                    "date": "2025-01-15",
                    "entry_description": "テスト",
                    "lines": [
                        {"account_code": accounts["5010"].code, "account_name": "消耗品費",
                         "debit_amount": 1000, "credit_amount": 0},
                        {"account_code": accounts["1010"].code, "account_name": "現金",
                         "debit_amount": 0, "credit_amount": 1000},
                    ],
                }],
            },
        ]

        from app.services.ai_receipt import analyze_and_suggest
        suggestions = analyze_and_suggest(user.id, b"fake", "image/jpeg")
        assert suggestions[0].compliance["status"] == "pass"
        assert suggestions[0].compliance["warnings"] == []
        assert len(suggestions[0].compliance["details"]) == 3

    @patch("app.services.ai_receipt._call_ai")
    def test_compliance_fail_result(self, mock_call, db, user, accounts):
        """AI が compliance fail を返すケースでも仕訳案は生成される"""
        self._setup_config(db, user.id, compliance_check=True)

        mock_call.side_effect = [
            {
                "date": None, "description": "",
                "amount": 0, "document_type": "other",
                "items": [], "needs_ledger": False,
                "requested_accounts": [],
                "compliance": {
                    "status": "fail",
                    "warnings": ["画像がぼやけており文字が読めません", "日付が読み取れません"],
                },
            },
            {
                "suggestions": [{
                    "title": "不明な支出",
                    "description": "desc",
                    "date": "2025-01-15",
                    "entry_description": "不明",
                    "lines": [
                        {"account_code": accounts["5010"].code, "account_name": "消耗品費",
                         "debit_amount": 1000, "credit_amount": 0},
                        {"account_code": accounts["1010"].code, "account_name": "現金",
                         "debit_amount": 0, "credit_amount": 1000},
                    ],
                }],
            },
        ]

        from app.services.ai_receipt import analyze_and_suggest
        suggestions = analyze_and_suggest(user.id, b"fake", "image/jpeg")
        # fail でも仕訳案は生成される（ユーザーが判断）
        assert suggestions[0].compliance["status"] == "fail"
        assert len(suggestions[0].compliance["warnings"]) == 2

    @patch("app.services.ai_receipt._call_ai")
    def test_compliance_missing_from_response(self, mock_call, db, user, accounts):
        """AI が compliance フィールドを返さなかった場合のフォールバック"""
        self._setup_config(db, user.id, compliance_check=True)

        mock_call.side_effect = [
            {
                "date": "2025-01-15", "description": "テスト",
                "amount": 1000, "document_type": "receipt",
                "items": [], "needs_ledger": False,
                "requested_accounts": [],
                # compliance フィールドなし
            },
            {
                "suggestions": [{
                    "title": "費用計上",
                    "description": "desc",
                    "date": "2025-01-15",
                    "entry_description": "テスト",
                    "lines": [
                        {"account_code": accounts["5010"].code, "account_name": "消耗品費",
                         "debit_amount": 1000, "credit_amount": 0},
                        {"account_code": accounts["1010"].code, "account_name": "現金",
                         "debit_amount": 0, "credit_amount": 1000},
                    ],
                }],
            },
        ]

        from app.services.ai_receipt import analyze_and_suggest
        suggestions = analyze_and_suggest(user.id, b"fake", "image/jpeg")
        # フォールバック: pass として扱う
        assert suggestions[0].compliance["status"] == "pass"
        assert suggestions[0].compliance["details"] == []


class TestSettingsComplianceToggle:
    """設定画面のコンプライアンスチェックトグル"""

    def test_save_compliance_on(self, db, logged_in_client, user):
        from app.services.ai_receipt import encrypt_api_key
        config = UserAIConfig(
            user_id=user.id,
            provider="openai",
            api_key_encrypted=encrypt_api_key("sk-test"),
        )
        db.session.add(config)
        db.session.commit()

        resp = logged_in_client.post("/settings/ai/save", data={
            "provider": "openai",
            "model_name": "gpt-4o",
            "custom_prompt": "",
            "base_url": "",
            "compliance_check": "on",
        }, follow_redirects=True)
        assert resp.status_code == 200

        updated = UserAIConfig.query.filter_by(user_id=user.id).first()
        assert updated.compliance_check is True

    def test_save_compliance_off(self, db, logged_in_client, user):
        from app.services.ai_receipt import encrypt_api_key
        config = UserAIConfig(
            user_id=user.id,
            provider="openai",
            api_key_encrypted=encrypt_api_key("sk-test"),
            compliance_check=True,
        )
        db.session.add(config)
        db.session.commit()

        resp = logged_in_client.post("/settings/ai/save", data={
            "provider": "openai",
            "model_name": "gpt-4o",
            "custom_prompt": "",
            "base_url": "",
            # compliance_check は送信しない → False
        }, follow_redirects=True)
        assert resp.status_code == 200

        updated = UserAIConfig.query.filter_by(user_id=user.id).first()
        assert updated.compliance_check is False


class TestReviewComplianceDisplay:
    """レビュー画面でコンプライアンス結果が表示されること"""

    def _setup_draft(self, db_sess, user_id, compliance):
        from app.models.ai_draft import AIDraft
        suggestions = [{
            "title": "テスト", "description": "desc",
            "date": "2025-01-15", "entry_description": "テスト",
            "lines": [
                {"account_code": "5010", "account_name": "消耗品費",
                 "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "account_name": "現金",
                 "debit_amount": 0, "credit_amount": 1000},
            ],
            "compliance": compliance,
        }]
        draft = AIDraft(
            user_id=user_id,
            image_key="test/key",
            image_mime="image/jpeg",
            suggestions_json=json.dumps(suggestions, ensure_ascii=False),
            status="analyzed",
        )
        db_sess.session.add(draft)
        db_sess.session.commit()
        return draft

    def test_review_pass_shows_ok_message(self, db, logged_in_client, user):
        compliance = {
            "status": "pass", "warnings": [],
            "details": ["画像品質: 鮮明", "必須情報: 確認済み"],
        }
        draft = self._setup_draft(db, user.id, compliance)
        resp = logged_in_client.get(
            f"/ai-journal/drafts/{draft.id}/review", follow_redirects=True,
        )
        html = resp.data.decode()
        assert "電帳法チェックOK" in html

    def test_review_pass_shows_detail_modal(self, db, logged_in_client, user):
        compliance = {
            "status": "pass", "warnings": [],
            "details": ["画像品質: 鮮明"],
        }
        draft = self._setup_draft(db, user.id, compliance)
        resp = logged_in_client.get(
            f"/ai-journal/drafts/{draft.id}/review", follow_redirects=True,
        )
        html = resp.data.decode()
        assert "complianceDetailModal" in html
        assert "画像品質: 鮮明" in html

    def test_review_warn_shows_detail_button(self, db, logged_in_client, user):
        compliance = {
            "status": "warn",
            "warnings": ["影あり"],
            "details": ["画像品質: 影あり", "必須情報: 確認済み"],
        }
        draft = self._setup_draft(db, user.id, compliance)
        resp = logged_in_client.get(
            f"/ai-journal/drafts/{draft.id}/review", follow_redirects=True,
        )
        html = resp.data.decode()
        assert "complianceDetailModal" in html
        assert "bi-info-circle" in html


class TestDraftsComplianceDisplay:
    """下書き一覧でコンプライアンスステータスが表示されること"""

    def test_drafts_shows_compliance_in_summary(self, db, logged_in_client, user):
        from app.models.ai_draft import AIDraft
        suggestions = [{
            "title": "テスト",
            "description": "desc",
            "date": "2025-01-15",
            "entry_description": "テスト",
            "lines": [
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
            ],
            "compliance": {"status": "warn", "warnings": ["影あり"]},
        }]
        draft = AIDraft(
            user_id=user.id,
            image_key="test/key",
            image_mime="image/jpeg",
            suggestions_json=json.dumps(suggestions, ensure_ascii=False),
            status="analyzed",
        )
        db.session.add(draft)
        db.session.commit()

        resp = logged_in_client.get("/ai-journal/drafts")
        assert resp.status_code == 200
        # 警告バッジが表示される
        assert "警告" in resp.data.decode()

    def test_drafts_shows_pass_badge(self, db, logged_in_client, user):
        from app.models.ai_draft import AIDraft
        suggestions = [{
            "title": "テスト",
            "description": "desc",
            "date": "2025-01-15",
            "entry_description": "テスト",
            "lines": [
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
            ],
            "compliance": {"status": "pass", "warnings": [], "details": ["OK"]},
        }]
        draft = AIDraft(
            user_id=user.id,
            image_key="test/key",
            image_mime="image/jpeg",
            suggestions_json=json.dumps(suggestions, ensure_ascii=False),
            status="analyzed",
        )
        db.session.add(draft)
        db.session.commit()

        resp = logged_in_client.get("/ai-journal/drafts")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "bi-check-circle" in html
