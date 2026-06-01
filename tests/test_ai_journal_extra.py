"""AI 証憑仕訳ビュー (ai_journal.py) の追加テスト

review は GET 専用 (登録はクライアント暗号化 → batch API 経由)。
image 配信・upload クリーンアップ・review レンダリングを網羅する。
"""

import io
import json
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.fiscal import FiscalClose
from app.models.voucher import Voucher


def _png_bytes() -> bytes:
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 50


def _ai_config(db, user_id):
    cfg = UserAIConfig(
        user_id=user_id, provider="openai",
        api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
        model_name="gpt-4",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _draft(db, user_id, *, status="analyzed", suggestions=None):
    if suggestions is None:
        suggestions = [{
            "title": "領収書", "description": "セブン",
            "date": "2026-02-15", "entry_description": "セブン",
            "lines": [
                {"account_code": "5010", "debit_amount": 500,
                 "credit_amount": 0, "description": ""},
                {"account_code": "1010", "debit_amount": 0,
                 "credit_amount": 500, "description": ""},
            ],
        }]
    d = AIDraft(
        user_id=user_id,
        image_key="drafts/1/test.png", image_mime="image/png",
        file_hash="h",
        suggestions_json=json.dumps(suggestions),
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(d)
    db.session.commit()
    return d


class TestAnalyzeEndpointRemoved:
    """/ai-journal/analyze (サーバ Fernet 経路) は廃止。
    POST すると 404 (ルート未定義) を返すことを担保。"""

    def test_analyze_endpoint_is_gone(self, logged_in_client, accounts):
        resp = logged_in_client.post("/ai-journal/analyze")
        assert resp.status_code == 404


class TestDraftsSave:
    def test_no_session_data(self, logged_in_client, accounts):
        resp = logged_in_client.post("/ai-journal/drafts/save")
        assert resp.status_code == 400

    def test_save_success(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id, status="temp")
        with logged_in_client.session_transaction() as sess:
            sess["ai_journal_draft_id"] = d.id
        resp = logged_in_client.post("/ai-journal/drafts/save")
        assert resp.status_code == 200
        db.session.refresh(d)
        assert d.status == "analyzed"

    def test_idor(self, db, logged_in_client, accounts, second_user):
        d = _draft(db, second_user.id, status="temp")
        with logged_in_client.session_transaction() as sess:
            sess["ai_journal_draft_id"] = d.id
        resp = logged_in_client.post("/ai-journal/drafts/save")
        assert resp.status_code == 400


class TestDraftImage:
    def test_unauthenticated(self, client):
        resp = client.get("/ai-journal/drafts/1/image")
        assert resp.status_code in (302, 401)

    def test_404_for_missing(self, logged_in_client, accounts):
        resp = logged_in_client.get("/ai-journal/drafts/9999/image")
        assert resp.status_code == 404

    def test_idor(self, db, logged_in_client, accounts, second_user):
        d = _draft(db, second_user.id)
        resp = logged_in_client.get(f"/ai-journal/drafts/{d.id}/image")
        assert resp.status_code == 403

    def test_serve_error_404(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id)
        with patch("app.views.ai_journal.serve_draft_image") as mock_serve:
            mock_serve.side_effect = FileNotFoundError("missing")
            resp = logged_in_client.get(f"/ai-journal/drafts/{d.id}/image")
            assert resp.status_code == 404


class TestVoucherImage:
    def test_unauthenticated(self, client):
        resp = client.get("/ai-journal/voucher/1/image")
        assert resp.status_code in (302, 401)

    def test_404(self, logged_in_client, accounts):
        resp = logged_in_client.get("/ai-journal/voucher/9999/image")
        assert resp.status_code == 404

    def test_idor(self, db, logged_in_client, accounts, second_user):
        from tests.conftest import make_voucher
        v = make_voucher(db, second_user.id)
        resp = logged_in_client.get(f"/ai-journal/voucher/{v.id}/image")
        assert resp.status_code == 403


class TestQuickAcceptRemoved:
    """E3-F PR-B3: drafts_quick_accept view 撤去。POST URL は 404 になる。"""

    def test_quick_accept_url_404(self, logged_in_client, accounts):
        resp = logged_in_client.post("/ai-journal/drafts/9999/quick-accept")
        assert resp.status_code == 404


class TestDraftsReview:
    def test_unauthenticated(self, client):
        resp = client.get("/ai-journal/drafts/1/review")
        assert resp.status_code in (302, 401)

    def test_404(self, logged_in_client, accounts):
        resp = logged_in_client.get("/ai-journal/drafts/9999/review")
        assert resp.status_code == 404

    def test_idor(self, db, logged_in_client, accounts, second_user):
        d = _draft(db, second_user.id)
        resp = logged_in_client.get(f"/ai-journal/drafts/{d.id}/review")
        assert resp.status_code in (302, 303)

    def test_no_suggestions(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id)
        d.suggestions_json = ""
        from app.extensions import db as _db
        _db.session.commit()
        resp = logged_in_client.get(f"/ai-journal/drafts/{d.id}/review")
        assert resp.status_code in (302, 303)

    def test_redirects_to_review(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id)
        resp = logged_in_client.get(f"/ai-journal/drafts/{d.id}/review")
        assert resp.status_code in (302, 303)
        assert "/review" in resp.headers.get("Location", "")


class TestReview:
    """review エンドポイント (GET 専用) のテスト"""

    def _setup(self, db, logged_in_client, user, suggestions=None):
        d = _draft(db, user.id, suggestions=suggestions)
        with logged_in_client.session_transaction() as sess:
            sess["ai_journal_draft_id"] = d.id
        return d

    def test_no_session(self, logged_in_client, accounts):
        resp = logged_in_client.get("/ai-journal/review", follow_redirects=False)
        assert resp.status_code == 302

    def test_get(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.get("/ai-journal/review")
        assert resp.status_code == 200
        assert "セブン".encode() in resp.data

    def test_get_embeds_draft_db_id(self, db, logged_in_client, user, accounts):
        # クライアント暗号化登録に使う実 draft id がページに埋め込まれる
        d = self._setup(db, logged_in_client, user)
        resp = logged_in_client.get("/ai-journal/review")
        assert resp.status_code == 200
        assert ("_reviewDraftId = %d" % d.id).encode() in resp.data

    def test_get_with_idx(self, db, logged_in_client, user, accounts):
        suggestions = [
            {"title": "案1", "description": "一件目の説明", "date": "2026-02-15",
             "entry_description": "一件目",
             "lines": [
                 {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                 {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
             ]},
            {"title": "案2", "description": "二件目の説明", "date": "2026-02-16",
             "entry_description": "二件目",
             "lines": [
                 {"account_code": "5010", "debit_amount": 200, "credit_amount": 0},
                 {"account_code": "1010", "debit_amount": 0, "credit_amount": 200},
             ]},
        ]
        self._setup(db, logged_in_client, user, suggestions=suggestions)
        resp = logged_in_client.get("/ai-journal/review?idx=1")
        assert resp.status_code == 200
        assert "二件目".encode() in resp.data

    def test_get_with_invalid_idx_clamps_to_zero(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.get("/ai-journal/review?idx=999")
        assert resp.status_code == 200
        assert "セブン".encode() in resp.data

    def test_post_not_allowed(self, db, logged_in_client, user, accounts):
        # 登録はクライアント暗号化 → batch API 経由。平文 POST は受け付けない。
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "simple",
            "date": "2026-02-15",
            "description": "x",
            "amount": "100",
            "category_account_code": "5010",
            "payment_account_code": "1010",
        })
        assert resp.status_code == 405

    def test_post_advanced_not_allowed(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "advanced",
            "date": "2026-02-15",
            "description": "x",
            "lines_json": json.dumps([
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ]),
        })
        assert resp.status_code == 405

    def test_get_idor_redirects(self, db, logged_in_client, accounts, second_user):
        # 他ユーザーの draft を指す session は draft=None 扱いで upload へ redirect
        d = _draft(db, second_user.id)
        with logged_in_client.session_transaction() as sess:
            sess["ai_journal_draft_id"] = d.id
        resp = logged_in_client.get("/ai-journal/review", follow_redirects=False)
        assert resp.status_code == 302
        assert "/ai-journal" in resp.headers["Location"]

    def test_get_deadline_exceeded(self, db, logged_in_client, user, accounts):
        # 受領日から大きく経過したレシートは入力期限超過の警告を表示する
        suggestions = [{
            "title": "案1", "date": "2000-01-01", "entry_description": "古い摘要",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        }]
        self._setup(db, logged_in_client, user, suggestions=suggestions)
        resp = logged_in_client.get("/ai-journal/review")
        assert resp.status_code == 200
        assert "入力期限超過の可能性".encode() in resp.data


class TestAiJournalE2EEBanner:
    """/ai-journal upload 画面のバナー分岐 (Fernet 経路廃止後)。

    - 設定なし → 「外部AI設定が登録されていません」warning
    - has_config=true, !is_e2ee (blob/iv 未保存) → 「E2EE モードに移行」warning
    - has_config=true, is_e2ee → 「E2EE モードで解析します」success
    """

    def test_non_e2ee_shows_migration_required_banner(
        self, db, logged_in_client, user, accounts,
    ):
        """api_key_blob/iv が未保存のユーザー → 再登録 warning + フォーム無効化。"""
        from app.models.ai_config import UserAIConfig
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_blob=None, api_key_iv=None,
            model_name="gpt-4o-mini",
        )
        from app.extensions import db as _db
        _db.session.add(cfg)
        _db.session.commit()
        resp = logged_in_client.get("/ai-journal/")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "クライアント完結の E2EE モードに移行しました" in html
        assert "E2EE 形式で再登録" in html
        # success バナーは出ない
        assert "E2EE モードで解析します" not in html

    def test_e2ee_success_banner_when_blob_iv_present(
        self, db, logged_in_client, user, accounts,
    ):
        """api_key_blob/iv 登録済 → success バナー + フォーム有効。"""
        from app.models.ai_config import UserAIConfig
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
            model_name="gpt-4o-mini",
        )
        from app.extensions import db as _db
        _db.session.add(cfg)
        _db.session.commit()
        resp = logged_in_client.get("/ai-journal/")
        html = resp.data.decode()
        assert "E2EE モードで解析します" in html
        assert "ブラウザから LLM に直接送信" in html
        # 非 E2EE 用 warning は出ない
        assert "クライアント完結の E2EE モードに移行しました" not in html

    def test_no_banner_when_no_config_at_all(
        self, db, logged_in_client, user, accounts,
    ):
        resp = logged_in_client.get("/ai-journal/")
        html = resp.data.decode()
        assert "E2EE モードで解析します" not in html
        assert "クライアント完結の E2EE モードに移行しました" not in html
        # 「外部AI設定が登録されていません」は出る
        assert "外部AI設定が登録されていません" in html
