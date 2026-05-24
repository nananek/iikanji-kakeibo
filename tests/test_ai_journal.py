"""AI証憑仕訳ビュー (ai_journal.py) のテスト"""

import json

import pytest

from app.extensions import db as _db
from app.models.ai_draft import AIDraft
from app.models.ai_config import UserAIConfig
from app.models.journal import JournalEntry
from app.models.user import User


def _make_suggestions(accounts, *, title="テスト仕訳", date="2026-01-15",
                      entry_description="テスト購入", compliance=None):
    s = {
        "title": title,
        "description": "desc",
        "date": date,
        "entry_description": entry_description,
        "lines": [
            {"account_code": accounts["5010"].code, "account_name": "食費",
             "debit_amount": 1000, "credit_amount": 0},
            {"account_code": accounts["1010"].code, "account_name": "現金",
             "debit_amount": 0, "credit_amount": 1000},
        ],
    }
    if compliance is not None:
        s["compliance"] = compliance
    return [s]


def _make_draft(db_sess, user_id, *, suggestions_json=None, status="analyzed",
                accounts=None):
    if suggestions_json is None and accounts is not None:
        suggestions_json = json.dumps(
            _make_suggestions(accounts), ensure_ascii=False,
        )
    draft = AIDraft(
        user_id=user_id,
        image_key=f"vouchers/{user_id}/test.jpg",
        image_mime="image/jpeg",
        suggestions_json=suggestions_json,
        status=status,
    )
    db_sess.session.add(draft)
    db_sess.session.commit()
    return draft


def _make_other_user(db_sess):
    other = User(username="other", email="other@example.com")
    other.set_password("pass")
    db_sess.session.add(other)
    db_sess.session.commit()
    return other


class TestUpload:
    """GET /ai-journal/ — アップロード画面"""

    def test_upload_no_ai_config(self, db, logged_in_client, user, account_types):
        resp = logged_in_client.get("/ai-journal/")
        assert resp.status_code == 200

    def test_upload_with_ai_config(self, db, logged_in_client, user, account_types):
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
            model_name="gpt-4o",
        )
        db.session.add(cfg)
        db.session.commit()
        resp = logged_in_client.get("/ai-journal/")
        assert resp.status_code == 200

    def test_upload_cleans_temp_drafts(self, db, logged_in_client, user, account_types):
        for _ in range(2):
            _make_draft(db, user.id, suggestions_json="[]", status="temp")
        assert AIDraft.query.filter_by(user_id=user.id, status="temp").count() == 2
        logged_in_client.get("/ai-journal/")
        assert AIDraft.query.filter_by(user_id=user.id, status="temp").count() == 0

    def test_upload_keeps_analyzed_drafts(self, db, logged_in_client, user,
                                          account_types, accounts):
        _make_draft(db, user.id, accounts=accounts, status="analyzed")
        logged_in_client.get("/ai-journal/")
        assert AIDraft.query.filter_by(user_id=user.id, status="analyzed").count() == 1

    def test_upload_clears_session_draft_id(self, db, logged_in_client, user,
                                             account_types):
        with logged_in_client.session_transaction() as sess:
            sess["ai_journal_draft_id"] = 9999
        logged_in_client.get("/ai-journal/")
        with logged_in_client.session_transaction() as sess:
            assert "ai_journal_draft_id" not in sess


class TestDrafts:
    """GET /ai-journal/drafts — 一時保存一覧"""

    def test_drafts_empty(self, db, logged_in_client, user, account_types):
        resp = logged_in_client.get("/ai-journal/drafts")
        assert resp.status_code == 200

    def test_drafts_shows_summary(self, db, logged_in_client, user,
                                   account_types, accounts):
        _make_draft(db, user.id, accounts=accounts)
        resp = logged_in_client.get("/ai-journal/drafts")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "テスト仕訳" in html
        assert "2026-01-15" in html
        assert "食費" in html
        assert "現金" in html

    def test_drafts_excludes_temp(self, db, logged_in_client, user,
                                   account_types, accounts):
        _make_draft(db, user.id, accounts=accounts, status="temp")
        resp = logged_in_client.get("/ai-journal/drafts")
        html = resp.data.decode()
        assert "テスト仕訳" not in html

    def test_drafts_excludes_other_user(self, db, logged_in_client, user,
                                         account_types, accounts):
        other = _make_other_user(db)
        _make_draft(db, other.id, accounts=accounts)
        resp = logged_in_client.get("/ai-journal/drafts")
        html = resp.data.decode()
        assert "テスト仕訳" not in html

    def test_drafts_handles_invalid_json(self, db, logged_in_client, user,
                                          account_types):
        _make_draft(db, user.id, suggestions_json="{bad json", status="analyzed")
        resp = logged_in_client.get("/ai-journal/drafts")
        assert resp.status_code == 200

    def test_drafts_handles_empty_suggestions(self, db, logged_in_client, user,
                                               account_types):
        _make_draft(db, user.id, suggestions_json="[]", status="analyzed")
        resp = logged_in_client.get("/ai-journal/drafts")
        assert resp.status_code == 200

    def test_drafts_handles_none_suggestions(self, db, logged_in_client, user,
                                              account_types):
        _make_draft(db, user.id, suggestions_json=None, status="analyzed")
        resp = logged_in_client.get("/ai-journal/drafts")
        assert resp.status_code == 200

    def test_drafts_compliance_summary(self, db, logged_in_client, user,
                                        account_types, accounts):
        suggestions = _make_suggestions(
            accounts,
            compliance={"status": "warn", "warnings": ["ピンぼけ"], "details": []},
        )
        _make_draft(db, user.id,
                    suggestions_json=json.dumps(suggestions, ensure_ascii=False),
                    status="analyzed")
        resp = logged_in_client.get("/ai-journal/drafts")
        assert resp.status_code == 200


class TestDraftsDelete:
    """POST /ai-journal/drafts/<id>/delete"""

    def test_delete_success(self, db, logged_in_client, user, account_types, accounts):
        draft = _make_draft(db, user.id, accounts=accounts)
        draft_id = draft.id
        resp = logged_in_client.post(
            f"/ai-journal/drafts/{draft_id}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert AIDraft.query.get(draft_id) is None

    def test_delete_other_user_rejected(self, db, logged_in_client, user,
                                         account_types, accounts):
        other = _make_other_user(db)
        draft = _make_draft(db, other.id, accounts=accounts)
        resp = logged_in_client.post(
            f"/ai-journal/drafts/{draft.id}/delete", follow_redirects=True)
        assert AIDraft.query.get(draft.id) is not None

    def test_delete_404(self, db, logged_in_client, user, account_types):
        resp = logged_in_client.post("/ai-journal/drafts/99999/delete")
        assert resp.status_code == 404


class TestDraftsReview:
    """GET /ai-journal/drafts/<id>/review"""

    def test_review_redirects(self, db, logged_in_client, user, account_types, accounts):
        draft = _make_draft(db, user.id, accounts=accounts)
        resp = logged_in_client.get(f"/ai-journal/drafts/{draft.id}/review")
        assert resp.status_code == 302
        assert "/ai-journal/review" in resp.headers["Location"]

    def test_review_sets_session(self, db, logged_in_client, user, account_types, accounts):
        draft = _make_draft(db, user.id, accounts=accounts)
        logged_in_client.get(f"/ai-journal/drafts/{draft.id}/review")
        with logged_in_client.session_transaction() as sess:
            assert sess["ai_journal_draft_id"] == draft.id

    def test_review_with_idx(self, db, logged_in_client, user, account_types, accounts):
        draft = _make_draft(db, user.id, accounts=accounts)
        resp = logged_in_client.get(f"/ai-journal/drafts/{draft.id}/review?idx=1")
        assert "idx=1" in resp.headers["Location"]

    def test_review_other_user_rejected(self, db, logged_in_client, user,
                                         account_types, accounts):
        other = _make_other_user(db)
        draft = _make_draft(db, other.id, accounts=accounts)
        resp = logged_in_client.get(
            f"/ai-journal/drafts/{draft.id}/review", follow_redirects=True)
        assert resp.status_code == 200

    def test_review_empty_suggestions(self, db, logged_in_client, user, account_types):
        draft = _make_draft(db, user.id, suggestions_json=None, status="analyzed")
        resp = logged_in_client.get(
            f"/ai-journal/drafts/{draft.id}/review", follow_redirects=True)
        assert resp.status_code == 200

    def test_review_404(self, db, logged_in_client, user, account_types):
        resp = logged_in_client.get("/ai-journal/drafts/99999/review")
        assert resp.status_code == 404


class TestQuickAcceptEdgeCases:
    """drafts_quick_accept の追加異常系"""

    def test_invalid_json_rejected(self, db, logged_in_client, user, account_types):
        draft = _make_draft(db, user.id, suggestions_json="{bad}", status="analyzed")
        resp = logged_in_client.post(
            f"/ai-journal/drafts/{draft.id}/quick-accept", follow_redirects=True)
        assert resp.status_code == 200
        assert JournalEntry.query.filter_by(source="ai_receipt").first() is None

    def test_no_suggestions_json(self, db, logged_in_client, user, account_types):
        draft = _make_draft(db, user.id, suggestions_json=None, status="analyzed")
        resp = logged_in_client.post(
            f"/ai-journal/drafts/{draft.id}/quick-accept", follow_redirects=True)
        assert resp.status_code == 200

    def test_missing_date(self, db, logged_in_client, user, account_types, accounts):
        suggestions = _make_suggestions(accounts, date="", entry_description="テスト")
        draft = _make_draft(db, user.id,
                            suggestions_json=json.dumps(suggestions, ensure_ascii=False),
                            status="analyzed")
        resp = logged_in_client.post(f"/ai-journal/drafts/{draft.id}/quick-accept")
        assert resp.status_code == 302

    def test_invalid_date(self, db, logged_in_client, user, account_types, accounts):
        suggestions = _make_suggestions(accounts, date="not-a-date")
        draft = _make_draft(db, user.id,
                            suggestions_json=json.dumps(suggestions, ensure_ascii=False),
                            status="analyzed")
        resp = logged_in_client.post(f"/ai-journal/drafts/{draft.id}/quick-accept")
        assert resp.status_code == 302

    def test_no_lines(self, db, logged_in_client, user, account_types):
        suggestions = [{"title": "t", "date": "2026-01-15",
                        "entry_description": "test", "lines": []}]
        draft = _make_draft(db, user.id,
                            suggestions_json=json.dumps(suggestions, ensure_ascii=False),
                            status="analyzed")
        resp = logged_in_client.post(f"/ai-journal/drafts/{draft.id}/quick-accept")
        assert resp.status_code == 302

    def test_404(self, db, logged_in_client, user, account_types):
        resp = logged_in_client.post("/ai-journal/drafts/99999/quick-accept")
        assert resp.status_code == 404
