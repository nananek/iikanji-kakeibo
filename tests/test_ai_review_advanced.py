"""AI証憑仕訳 仕訳モード（advanced）のテスト"""

import json

import pytest

from app.extensions import db
from app.models.ai_draft import AIDraft
from app.models.journal import JournalEntry
from app.models.voucher import Voucher


class TestAdvancedModePost:
    """仕訳モードでの POST 処理"""

    def _make_draft(self, db_sess, user_id, accounts):
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
            status="analyzed",
        )
        db_sess.session.add(draft)
        db_sess.session.commit()
        return draft

    def _set_draft_session(self, client, draft):
        with client.session_transaction() as sess:
            sess["ai_journal_draft_id"] = draft.id

    def test_advanced_mode_creates_entry(self, db, logged_in_client, user, accounts):
        """仕訳モードで正常に仕訳を作成できる"""
        draft = self._make_draft(db, user.id, accounts)
        self._set_draft_session(logged_in_client, draft)

        lines_json = json.dumps([
            {"account_code": accounts["5010"].code, "debit_amount": 1000, "credit_amount": 0, "description": ""},
            {"account_code": accounts["1010"].code, "debit_amount": 0, "credit_amount": 1000, "description": ""},
        ])

        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "advanced",
            "date": "2026-01-15",
            "description": "テスト購入",
            "lines_json": lines_json,
        }, follow_redirects=True)
        assert resp.status_code == 200

        entry = JournalEntry.query.filter_by(source="ai_receipt").first()
        assert entry is not None
        assert entry.description == "テスト購入"
        assert len(entry.lines) == 2

    def _assert_flash(self, resp, message):
        """flash メッセージが tojson でエスケープされた形式で存在することを確認"""
        html = resp.data.decode()
        # Jinja2 の |tojson は日本語を \uXXXX にエスケープする
        assert json.dumps(message) in html

    def test_advanced_mode_empty_lines(self, db, logged_in_client, user, accounts):
        """空の明細で送信するとエラー"""
        draft = self._make_draft(db, user.id, accounts)
        self._set_draft_session(logged_in_client, draft)

        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "advanced",
            "date": "2026-01-15",
            "description": "テスト",
            "lines_json": "[]",
        }, follow_redirects=True)
        assert resp.status_code == 200
        self._assert_flash(resp, "仕訳明細を入力してください。")

    def test_advanced_mode_invalid_json(self, db, logged_in_client, user, accounts):
        """不正な JSON で送信するとエラー"""
        draft = self._make_draft(db, user.id, accounts)
        self._set_draft_session(logged_in_client, draft)

        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "advanced",
            "date": "2026-01-15",
            "description": "テスト",
            "lines_json": "{invalid}",
        }, follow_redirects=True)
        assert resp.status_code == 200
        self._assert_flash(resp, "明細データが不正です。")

    def test_advanced_mode_missing_date(self, db, logged_in_client, user, accounts):
        """日付なしで送信するとエラー"""
        draft = self._make_draft(db, user.id, accounts)
        self._set_draft_session(logged_in_client, draft)

        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "advanced",
            "date": "",
            "description": "テスト",
            "lines_json": "[]",
        }, follow_redirects=True)
        assert resp.status_code == 200
        self._assert_flash(resp, "日付と摘要を入力してください。")

    def test_advanced_mode_no_session(self, db, logged_in_client, user):
        """セッションにドラフトIDがない場合はリダイレクト"""
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "advanced",
            "date": "2026-01-15",
            "description": "テスト",
            "lines_json": "[]",
        }, follow_redirects=True)
        assert resp.status_code == 200
        self._assert_flash(resp, "AI解析データがありません。もう一度アップロードしてください。")

    def test_review_page_renders_advanced_tab(self, db, logged_in_client, user, accounts):
        """レビュー画面に仕訳モードタブが表示される"""
        draft = self._make_draft(db, user.id, accounts)
        self._set_draft_session(logged_in_client, draft)

        resp = logged_in_client.get("/ai-journal/review")
        html = resp.data.decode()
        assert "advancedMode" in html
        assert "仕訳モード" in html
        assert "lines_json" in html


class TestQuickAccept:
    """下書き一覧からの案1クイックアクセプト"""

    def _make_draft(self, db_sess, user_id, accounts, status="analyzed"):
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

    def test_quick_accept_creates_entry(self, db, logged_in_client, user, accounts):
        """案1でクイックアクセプトすると仕訳が作成される"""
        draft = self._make_draft(db, user.id, accounts)

        resp = logged_in_client.post(
            f"/ai-journal/drafts/{draft.id}/quick-accept",
            follow_redirects=True,
        )
        assert resp.status_code == 200

        entry = JournalEntry.query.filter_by(source="ai_receipt").first()
        assert entry is not None
        assert entry.description == "テスト購入"
        assert len(entry.lines) == 2

        # ドラフトが削除され、Voucher が作成されている
        assert AIDraft.query.get(draft.id) is None
        voucher = Voucher.query.filter_by(journal_entry_id=entry.id).first()
        assert voucher is not None

    def test_quick_accept_done_draft_rejected(self, db, logged_in_client, user, accounts):
        """仕訳登録済みのドラフトはクイックアクセプトできない"""
        draft = self._make_draft(db, user.id, accounts, status="done")

        resp = logged_in_client.post(
            f"/ai-journal/drafts/{draft.id}/quick-accept",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert JournalEntry.query.filter_by(source="ai_receipt").first() is None

    def test_quick_accept_other_user_rejected(self, db, logged_in_client, user, accounts):
        """他ユーザーのドラフトはクイックアクセプトできない"""
        from app.models.user import User
        other = User(username="other", email="other@example.com")
        other.set_password("pass")
        db.session.add(other)
        db.session.commit()

        draft = self._make_draft(db, other.id, accounts)

        resp = logged_in_client.post(
            f"/ai-journal/drafts/{draft.id}/quick-accept",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert JournalEntry.query.filter_by(source="ai_receipt").first() is None

    def test_quick_accept_no_suggestions(self, db, logged_in_client, user):
        """suggestions が空のドラフトはクイックアクセプトできない"""
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
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert JournalEntry.query.filter_by(source="ai_receipt").first() is None

    def test_drafts_page_shows_quick_accept_button(self, db, logged_in_client, user, accounts):
        """下書き一覧に「案1で登録」ボタンが表示される"""
        self._make_draft(db, user.id, accounts)

        resp = logged_in_client.get("/ai-journal/drafts")
        html = resp.data.decode()
        assert "案1で登録" in html
        assert "quick-accept" in html


class TestReviewButtons:
    """レビュー画面のボタン表示テスト"""

    def _make_draft(self, db_sess, user_id, accounts, status="analyzed"):
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

    def test_temp_draft_shows_save_draft_button(self, db, logged_in_client, user, accounts):
        """temp ドラフトのレビュー画面に「下書き保存」ボタンが表示される"""
        draft = self._make_draft(db, user.id, accounts, status="temp")
        with logged_in_client.session_transaction() as sess:
            sess["ai_journal_draft_id"] = draft.id

        resp = logged_in_client.get("/ai-journal/review")
        html = resp.data.decode()
        assert "下書き保存" in html
        assert "saveDraftFromReview" in html

    def test_temp_draft_shows_retry_with_confirm(self, db, logged_in_client, user, accounts):
        """temp ドラフトの「やり直す」に confirm が付いている"""
        draft = self._make_draft(db, user.id, accounts, status="temp")
        with logged_in_client.session_transaction() as sess:
            sess["ai_journal_draft_id"] = draft.id

        resp = logged_in_client.get("/ai-journal/review")
        html = resp.data.decode()
        assert "やり直す" in html
        assert "confirm(" in html

    def test_saved_draft_shows_back_to_list(self, db, logged_in_client, user, accounts):
        """analyzed ドラフトのレビュー画面に「一覧に戻る」が表示される（下書き保存なし）"""
        draft = self._make_draft(db, user.id, accounts, status="analyzed")
        with logged_in_client.session_transaction() as sess:
            sess["ai_journal_draft_id"] = draft.id

        resp = logged_in_client.get("/ai-journal/review")
        html = resp.data.decode()
        assert "一覧に戻る" in html
        assert "下書き保存" not in html
        assert "やり直す" not in html
