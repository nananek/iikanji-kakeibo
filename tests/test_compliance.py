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
        config = UserAIConfig(
            user_id=user.id,
            provider="openai",
            api_key_blob=b"\xAA" * 48,
            api_key_iv=b"\xBB" * 12,
        )
        db.session.add(config)
        db.session.commit()
        assert config.compliance_check is False

    def test_can_enable(self, db, user):
        config = UserAIConfig(
            user_id=user.id,
            provider="openai",
            api_key_blob=b"\xAA" * 48,
            api_key_iv=b"\xBB" * 12,
            compliance_check=True,
        )
        db.session.add(config)
        db.session.commit()
        loaded = UserAIConfig.query.filter_by(user_id=user.id).first()
        assert loaded.compliance_check is True


# TestCompliancePromptInjection (旧 analyze_and_suggest 経由の
# サーバ側プロンプト注入テスト) は対応関数の削除に伴い削除済。
# クライアント側 round1.js + /api/v1/ai/prompt-context endpoint での
# COMPLIANCE_CHECK_PROMPT 配信は tests/test_ai_uploads_api.py の
# TestAiPromptContext と tests/static/js/test_round1.mjs でカバー済。


# 旧 form POST /settings/ai/save 経由の compliance_check トグルテストは
# E2EE 化に伴いエンドポイント廃止のため削除。PUT /api/v1/ai-config 経由の
# compliance_check 保存テストは tests/test_ai_config_api.py で代替済。


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
