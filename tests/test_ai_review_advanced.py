"""AI証憑仕訳 仕訳モード（advanced）のテスト

登録はクライアント暗号化 → batch API (draft_id 連携) 経路に移行済み。
review への平文 POST は 405。ここでは GET レンダリングと POST 拒否を確認する。
"""

import json

import pytest

from app.extensions import db
from app.models.ai_draft import AIDraft
from app.models.journal import JournalEntry


def _make_draft(db_sess, user_id, accounts, status="analyzed"):
    suggestions = [{
        "title": "テスト仕訳",
        "description": "desc",
        "date": "2026-01-15",
        "entry_description": "テスト購入",
        "lines": [
            {"account_code": accounts["5010"].code, "account_name": "食費",
             "debit_amount": 1000, "credit_amount": 0},
            {"account_code": accounts["1010"].code, "account_name": "現金",
             "debit_amount": 0, "credit_amount": 1000},
        ],
    }]
    draft = AIDraft(
        user_id=user_id,
        image_key="vouchers/1/test.jpg",
        image_mime="image/jpeg",
        suggestions_json=json.dumps(suggestions, ensure_ascii=False),
        status=status,
    )
    db_sess.session.add(draft)
    db_sess.session.commit()
    return draft


def _set_draft_session(client, draft):
    with client.session_transaction() as sess:
        sess["ai_journal_draft_id"] = draft.id


class TestAdvancedModePostRejected:
    """仕訳モードの平文 POST は受け付けない (クライアント暗号化経路へ移行)"""

    def test_advanced_mode_post_405(self, db, logged_in_client, user, accounts):
        draft = _make_draft(db, user.id, accounts)
        _set_draft_session(logged_in_client, draft)
        lines_json = json.dumps([
            {"account_code": accounts["5010"].code, "debit_amount": 1000, "credit_amount": 0, "description": ""},
            {"account_code": accounts["1010"].code, "debit_amount": 0, "credit_amount": 1000, "description": ""},
        ])
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "advanced",
            "date": "2026-01-15",
            "description": "テスト購入",
            "lines_json": lines_json,
        })
        assert resp.status_code == 405
        # 平文 WRITE は行われない
        assert JournalEntry.query.filter_by(source="ai_receipt").first() is None

    def test_simple_mode_post_405(self, db, logged_in_client, user, accounts):
        draft = _make_draft(db, user.id, accounts)
        _set_draft_session(logged_in_client, draft)
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "simple",
            "date": "2026-01-15",
            "description": "テスト購入",
            "amount": "1000",
            "category_account_code": accounts["5010"].code,
            "payment_account_code": accounts["1010"].code,
        })
        assert resp.status_code == 405
        assert JournalEntry.query.filter_by(source="ai_receipt").first() is None


class TestReviewPageRender:
    """review GET 画面のクライアント暗号化登録に必要な要素のレンダリング"""

    def test_page_renders_advanced_tab(self, db, logged_in_client, user, accounts):
        draft = _make_draft(db, user.id, accounts)
        _set_draft_session(logged_in_client, draft)
        resp = logged_in_client.get("/ai-journal/review")
        html = resp.data.decode()
        assert "advancedMode" in html
        assert "仕訳モード" in html
        # 暗号化 submit ハンドラと userId が埋め込まれている
        assert "submitReviewAdvanced" in html
        assert "_userId" in html
        assert "aiReviewSubmitE2EE" in html

    def test_advanced_lines_prefilled(self, db, logged_in_client, user, accounts):
        draft = _make_draft(db, user.id, accounts)
        _set_draft_session(logged_in_client, draft)
        resp = logged_in_client.get("/ai-journal/review?tab=advanced")
        html = resp.data.decode()
        assert accounts["5010"].code in html
        assert "テスト購入" in html


class TestQuickAcceptRemoved:
    """drafts_quick_accept view は撤去、案 1 登録は batch API
    + entry-level draft_id 経路に置き換え。残置確認用テスト。
    """

    def test_quick_accept_url_returns_404(self, db, logged_in_client, user, accounts):
        draft = AIDraft(
            user_id=user.id,
            image_key="vouchers/1/test.jpg",
            image_mime="image/jpeg",
            suggestions_json="[]",
            status="analyzed",
        )
        db.session.add(draft)
        db.session.commit()
        resp = logged_in_client.post(
            f"/ai-journal/drafts/{draft.id}/quick-accept",
        )
        assert resp.status_code == 404


class TestReviewButtons:
    """レビュー画面のボタン表示テスト"""

    def test_saved_draft_shows_back_to_list(self, db, logged_in_client, user, accounts):
        """analyzed ドラフトのレビュー画面に「一覧に戻る」が表示される（下書き保存なし）"""
        draft = _make_draft(db, user.id, accounts, status="analyzed")
        _set_draft_session(logged_in_client, draft)
        resp = logged_in_client.get("/ai-journal/review")
        html = resp.data.decode()
        assert "一覧に戻る" in html
        assert "下書き保存" not in html
        assert "やり直す" not in html

    def test_temp_draft_shows_save_draft_button(self, db, logged_in_client, user, accounts):
        """temp ドラフトのレビュー画面に「下書き保存」「やり直す」ボタンが表示される"""
        draft = _make_draft(db, user.id, accounts, status="temp")
        _set_draft_session(logged_in_client, draft)
        resp = logged_in_client.get("/ai-journal/review")
        html = resp.data.decode()
        assert "下書き保存" in html
        assert "saveDraftFromReview" in html
        assert "やり直す" in html
        assert "confirm(" in html
