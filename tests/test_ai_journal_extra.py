"""AI 証憑仕訳ビュー (ai_journal.py) の追加テスト

未到達範囲: image 配信、quick_accept 各エラーパス、
review POST (simple/advanced モード)、_update_discord_done など。
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
    from app.services.ai_receipt import encrypt_api_key
    cfg = UserAIConfig(
        user_id=user_id, provider="openai",
        api_key_encrypted=encrypt_api_key("k"), model_name="gpt-4",
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
    """E2 PR-C-4e: /ai-journal/analyze (サーバ Fernet 経路) は廃止。
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
        with patch("app.views.ai_journal.serve_image") as mock_serve:
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


class TestQuickAccept:
    def test_unauthenticated(self, client):
        resp = client.post("/ai-journal/drafts/1/quick-accept")
        assert resp.status_code in (302, 401)

    def test_404(self, logged_in_client, accounts):
        resp = logged_in_client.post("/ai-journal/drafts/9999/quick-accept")
        assert resp.status_code == 404

    def test_idor(self, db, logged_in_client, accounts, second_user):
        d = _draft(db, second_user.id)
        resp = logged_in_client.post(f"/ai-journal/drafts/{d.id}/quick-accept")
        assert resp.status_code in (302, 303)
        # 仕訳は作られない
        from app.models.journal import JournalEntry
        assert JournalEntry.query.count() == 0

    def test_already_done(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id, status="done")
        resp = logged_in_client.post(f"/ai-journal/drafts/{d.id}/quick-accept")
        assert resp.status_code in (302, 303)

    def test_no_suggestions(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id)
        d.suggestions_json = ""
        from app.extensions import db as _db
        _db.session.commit()
        resp = logged_in_client.post(f"/ai-journal/drafts/{d.id}/quick-accept")
        assert resp.status_code in (302, 303)

    def test_invalid_json(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id)
        d.suggestions_json = "not-json{"
        from app.extensions import db as _db
        _db.session.commit()
        resp = logged_in_client.post(f"/ai-journal/drafts/{d.id}/quick-accept")
        assert resp.status_code in (302, 303)

    def test_missing_date_redirects_to_review(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id, suggestions=[{
            "date": "", "entry_description": "x", "lines": [],
        }])
        resp = logged_in_client.post(f"/ai-journal/drafts/{d.id}/quick-accept")
        assert resp.status_code in (302, 303)
        assert f"/drafts/{d.id}/review" in resp.headers.get("Location", "") or \
               "review" in resp.headers.get("Location", "")

    def test_invalid_date(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id, suggestions=[{
            "date": "BAD-DATE", "entry_description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        }])
        resp = logged_in_client.post(f"/ai-journal/drafts/{d.id}/quick-accept")
        assert resp.status_code in (302, 303)

    def test_no_lines(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id, suggestions=[{
            "date": "2026-02-15", "entry_description": "x",
            "lines": [],
        }])
        resp = logged_in_client.post(f"/ai-journal/drafts/{d.id}/quick-accept")
        assert resp.status_code in (302, 303)

    def test_locked_period(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(f"/ai-journal/drafts/{d.id}/quick-accept")
        assert resp.status_code in (302, 303)

    def test_success(self, db, logged_in_client, user, accounts):
        d = _draft(db, user.id)
        with patch("app.views.ai_journal.create_voucher_from_draft"):
            resp = logged_in_client.post(f"/ai-journal/drafts/{d.id}/quick-accept")
        assert resp.status_code in (302, 303)
        from app.models.journal import JournalEntry
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="ai_receipt"
        ).count() == 1


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
    def _setup(self, db, logged_in_client, user, suggestions=None):
        d = _draft(db, user.id, suggestions=suggestions)
        with logged_in_client.session_transaction() as sess:
            sess["ai_journal_draft_id"] = d.id
        return d

    def test_no_session(self, logged_in_client, accounts):
        resp = logged_in_client.get("/ai-journal/review")
        assert resp.status_code in (302, 303)

    def test_get(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.get("/ai-journal/review")
        assert resp.status_code == 200

    def test_get_with_idx(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.get("/ai-journal/review?idx=0")
        assert resp.status_code == 200

    def test_get_with_invalid_idx_clamps_to_zero(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.get("/ai-journal/review?idx=999")
        assert resp.status_code == 200

    def test_post_simple_mode_success(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        with patch("app.views.ai_journal.create_voucher_from_draft"):
            resp = logged_in_client.post("/ai-journal/review", data={
                "mode": "simple",
                "date": "2026-02-15",
                "description": "ファミマ",
                "amount": "300",
                "category_account_code": "5010",
                "payment_account_code": "1010",
            })
        assert resp.status_code in (302, 303)
        from app.models.journal import JournalEntry
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="ai_receipt"
        ).count() == 1

    def test_post_simple_missing_required(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "simple",
            "date": "",
            "description": "",
        })
        # 200 でフォーム再表示
        assert resp.status_code == 200

    def test_post_simple_missing_amount(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "simple",
            "date": "2026-02-15",
            "description": "x",
            "amount": "0",
            "category_account_code": "",
            "payment_account_code": "",
        })
        assert resp.status_code == 200

    def test_post_invalid_date(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "simple",
            "date": "BAD",
            "description": "x",
        })
        assert resp.status_code == 200

    def test_post_advanced_mode(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        with patch("app.views.ai_journal.create_voucher_from_draft"):
            resp = logged_in_client.post("/ai-journal/review", data={
                "mode": "advanced",
                "date": "2026-02-15",
                "description": "詳細モード",
                "lines_json": json.dumps([
                    {"account_code": "5010", "debit_amount": 200, "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0, "credit_amount": 200},
                ]),
            })
        assert resp.status_code in (302, 303)

    def test_post_advanced_invalid_lines(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "advanced",
            "date": "2026-02-15",
            "description": "x",
            "lines_json": "not-json{",
        })
        assert resp.status_code == 200

    def test_post_no_lines(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "advanced",
            "date": "2026-02-15",
            "description": "x",
            "lines_json": json.dumps([]),
        })
        assert resp.status_code == 200

    def test_post_locked_period(self, db, logged_in_client, user, accounts):
        self._setup(db, logged_in_client, user)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post("/ai-journal/review", data={
            "mode": "simple",
            "date": "2026-02-15",
            "description": "x",
            "amount": "100",
            "category_account_code": "5010",
            "payment_account_code": "1010",
        })
        assert resp.status_code == 200


class TestUpdateDiscordDone:
    """_update_discord_done のロジック (Webhook 編集)"""

    def test_no_discord_url(self, db, user, accounts):
        d = _draft(db, user.id)
        # 設定なし → 何も起きない
        from app.views.ai_journal import _update_discord_done
        _update_discord_done(d, 1)

    def test_with_discord_url(self, db, user, accounts):
        d = _draft(db, user.id)
        d.discord_webhook_url = "https://discord.com/api/webhooks/x/y"
        d.discord_message_id = "msg-1"
        from app.extensions import db as _db
        _db.session.commit()
        with patch("app.services.notify.update_discord_message") as mock_upd:
            from app.views.ai_journal import _update_discord_done
            _update_discord_done(d, 99)
            mock_upd.assert_called_once()

    def test_invalid_suggestions_json(self, db, user, accounts):
        d = _draft(db, user.id)
        d.discord_webhook_url = "https://x"
        d.discord_message_id = "msg"
        d.suggestions_json = "not-json{"
        from app.extensions import db as _db
        _db.session.commit()
        with patch("app.services.notify.update_discord_message") as mock_upd:
            from app.views.ai_journal import _update_discord_done
            _update_discord_done(d, 99)
            # 例外を起こさず呼ばれる
            mock_upd.assert_called_once()


class TestAiJournalE2EEBanner:
    """E2 PR-C-4e: /ai-journal upload 画面のバナー分岐 (Fernet 経路廃止後)。

    - 設定なし → 「外部AI設定が登録されていません」warning
    - has_config=true, !is_e2ee (legacy Fernet のみ) → 「E2EE モードに移行」warning
    - has_config=true, is_e2ee (移行期間混在含む) → 「E2EE モードで解析します」success
    """

    def test_legacy_only_shows_migration_required_banner(
        self, db, logged_in_client, user, accounts,
    ):
        """Fernet 形式のみ登録のユーザー → 再登録を促す warning が出てフォームは無効化。"""
        from app.models.ai_config import UserAIConfig
        from app.services.ai_receipt import encrypt_api_key
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_encrypted=encrypt_api_key("sk-legacy"),
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
        """api_key_blob/iv 登録済 (Fernet 残存有無を問わず) → success バナー + フォーム有効。"""
        from app.models.ai_config import UserAIConfig
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_encrypted=None,
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
        # legacy 用 warning は出ない
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
