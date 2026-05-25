"""REST API (api.py) の追加テスト

既存 test_api.py は journals 系を中心。こちらは ai/analyze, ai/drafts,
vouchers エンドポイントをカバー。
"""

import io
from datetime import date
from unittest.mock import MagicMock, patch

from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from tests.conftest import _auth_header, make_journal, make_voucher


def _png_bytes() -> bytes:
    """テスト用の最小 PNG"""
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        # PIL なくても署名で通る形にする
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


def _setup_ai_config(db, user_id):
    """AI 設定を雛形で作る (E2EE 形式)"""
    cfg = UserAIConfig(
        user_id=user_id, provider="openai",
        api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
        model_name="gpt-4",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


class TestAiAnalyzeRemoved:
    """Bearer API /api/v1/ai/analyze (Fernet 復号 + サーバ LLM
    呼出し経路) は廃止。POST すると 404 を返すことを担保。

    クライアントは 2-step フローに移行:
      1. POST /api/v1/ai/uploads (画像 + comment) → draft_id
      2. クライアント側で LLM 呼出 → PATCH /api/v1/ai/drafts/<id>/suggestions
    """

    def test_no_auth_returns_404(self, client):
        # ルート自体が無いため認証チェック前のルート解決で 404
        resp = client.post("/api/v1/ai/analyze")
        assert resp.status_code == 404

    def test_authed_post_returns_404(
        self, db, client, user, auth_header, accounts,
    ):
        _setup_ai_config(db, user.id)
        resp = client.post(
            "/api/v1/ai/analyze",
            headers=auth_header,
            data={"image": (io.BytesIO(_png_bytes()), "x.png", "image/png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 404


class TestAiDrafts:
    def _make_draft(self, db, user_id, status="analyzed"):
        d = AIDraft(
            user_id=user_id, image_key="drafts/1/x.png", image_mime="image/png",
            file_hash="h", suggestions_json="[]", status=status,
        )
        db.session.add(d)
        db.session.commit()
        return d

    def test_no_auth(self, client):
        resp = client.get("/api/v1/ai/drafts")
        assert resp.status_code == 401

    def test_list_analyzed(self, db, client, user, auth_header, accounts):
        self._make_draft(db, user.id, "analyzed")
        self._make_draft(db, user.id, "done")
        resp = client.get("/api/v1/ai/drafts", headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()
        # デフォルト status=analyzed
        assert body["total"] == 1

    def test_list_all(self, db, client, user, auth_header, accounts):
        self._make_draft(db, user.id, "analyzed")
        self._make_draft(db, user.id, "done")
        resp = client.get("/api/v1/ai/drafts?status=all", headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 2

    def test_list_invalid_status(self, client, auth_header, accounts):
        resp = client.get("/api/v1/ai/drafts?status=BAD", headers=auth_header)
        assert resp.status_code == 400

    def test_get_detail(self, db, client, user, auth_header, accounts):
        d = self._make_draft(db, user.id)
        resp = client.get(f"/api/v1/ai/drafts/{d.id}", headers=auth_header)
        assert resp.status_code == 200

    def test_get_detail_404(self, client, auth_header, accounts):
        resp = client.get("/api/v1/ai/drafts/9999", headers=auth_header)
        assert resp.status_code == 404

    def test_get_detail_temp_404(self, db, client, user, auth_header, accounts):
        d = self._make_draft(db, user.id, status="temp")
        resp = client.get(f"/api/v1/ai/drafts/{d.id}", headers=auth_header)
        assert resp.status_code == 404

    def test_delete_draft(self, db, client, user, auth_header, accounts):
        d = self._make_draft(db, user.id)
        did = d.id
        with patch("app.views.api.get_storage_backend") as mock_storage:
            mock_storage.return_value = MagicMock()
            resp = client.delete(f"/api/v1/ai/drafts/{did}",
                                 headers=auth_header)
        assert resp.status_code == 200
        assert db.session.get(AIDraft, did) is None

    def test_delete_404(self, client, auth_header, accounts):
        resp = client.delete("/api/v1/ai/drafts/9999", headers=auth_header)
        assert resp.status_code == 404

    def test_idor_other_user_draft(self, db, client, user, auth_header, accounts,
                                    second_user):
        d = self._make_draft(db, second_user.id)
        resp = client.get(f"/api/v1/ai/drafts/{d.id}", headers=auth_header)
        assert resp.status_code == 404


class TestVouchersList:
    def test_no_auth(self, client):
        resp = client.get("/api/v1/vouchers")
        assert resp.status_code == 401

    def test_empty(self, client, auth_header, accounts):
        resp = client.get("/api/v1/vouchers", headers=auth_header)
        assert resp.status_code == 200
        assert resp.get_json()["vouchers"] == []

    def test_list_with_journal(self, db, client, user, auth_header, accounts):
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15), source="ai_receipt")
        v = make_voucher(db, user.id, journal_entry_id=entry.id)
        resp = client.get("/api/v1/vouchers", headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 1
        assert body["vouchers"][0]["id"] == v.id
        assert body["vouchers"][0]["journal"] is not None

    def test_orphaned_voucher(self, db, client, user, auth_header, accounts):
        v = make_voucher(db, user.id, journal_entry_id=None)
        resp = client.get("/api/v1/vouchers", headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 1
        assert body["vouchers"][0]["journal"] is None

    def test_date_from_filter(self, db, client, user, auth_header, accounts):
        e1 = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 1, 15), source="ai_receipt")
        e2 = make_journal(db, user.id, "5010", "1010", 200,
                          entry_date=date(2026, 2, 15), source="ai_receipt")
        make_voucher(db, user.id, journal_entry_id=e1.id, image_key="vouchers/1/1.jpg")
        make_voucher(db, user.id, journal_entry_id=e2.id, image_key="vouchers/1/2.jpg")
        resp = client.get("/api/v1/vouchers?date_from=2026-02-01",
                          headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 1

    def test_invalid_date_from(self, client, auth_header, accounts):
        resp = client.get("/api/v1/vouchers?date_from=BAD",
                          headers=auth_header)
        assert resp.status_code == 400

    def test_search_filter(self, db, client, user, auth_header, accounts):
        e1 = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 2, 15), source="ai_receipt",
                          description="セブン")
        e2 = make_journal(db, user.id, "5010", "1010", 200,
                          entry_date=date(2026, 2, 16), source="ai_receipt",
                          description="ファミマ")
        make_voucher(db, user.id, journal_entry_id=e1.id, image_key="vouchers/1/1.jpg")
        make_voucher(db, user.id, journal_entry_id=e2.id, image_key="vouchers/1/2.jpg")
        resp = client.get("/api/v1/vouchers?search=セブン",
                          headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 1

    def test_amount_filter(self, db, client, user, auth_header, accounts):
        e1 = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 2, 15), source="ai_receipt")
        e2 = make_journal(db, user.id, "5010", "1010", 5000,
                          entry_date=date(2026, 2, 16), source="ai_receipt")
        make_voucher(db, user.id, journal_entry_id=e1.id, image_key="vouchers/1/1.jpg")
        make_voucher(db, user.id, journal_entry_id=e2.id, image_key="vouchers/1/2.jpg")
        resp = client.get("/api/v1/vouchers?amount_from=1000",
                          headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 1


class TestVoucherImage:
    def test_no_auth(self, client):
        resp = client.get("/api/v1/vouchers/1/image")
        assert resp.status_code == 401

    def test_404(self, client, auth_header, accounts):
        resp = client.get("/api/v1/vouchers/9999/image",
                          headers=auth_header)
        assert resp.status_code == 404

    def test_idor_other_user(self, db, client, user, auth_header, accounts,
                              second_user):
        v = make_voucher(db, second_user.id)
        resp = client.get(f"/api/v1/vouchers/{v.id}/image",
                          headers=auth_header)
        assert resp.status_code == 404


class TestVoucherVerify:
    def test_no_auth(self, client):
        resp = client.get("/api/v1/vouchers/1/verify")
        assert resp.status_code == 401

    def test_404(self, client, auth_header, accounts):
        resp = client.get("/api/v1/vouchers/9999/verify",
                          headers=auth_header)
        assert resp.status_code == 404

    def test_no_hash_recorded(self, db, client, user, auth_header, accounts):
        v = make_voucher(db, user.id)
        v.file_hash = None
        db.session.commit()
        resp = client.get(f"/api/v1/vouchers/{v.id}/verify",
                          headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["verified"] is None

    def test_hash_match(self, db, client, user, auth_header, accounts):
        import hashlib
        data = b"image-content"
        v = make_voucher(db, user.id)
        v.file_hash = hashlib.sha256(data).hexdigest()
        db.session.commit()
        with patch("app.views.api.get_storage_backend") as mock_storage:
            backend = MagicMock()
            backend.get.return_value = data
            mock_storage.return_value = backend
            resp = client.get(f"/api/v1/vouchers/{v.id}/verify",
                              headers=auth_header)
        body = resp.get_json()
        assert body["verified"] is True

    def test_hash_mismatch(self, db, client, user, auth_header, accounts):
        v = make_voucher(db, user.id)
        v.file_hash = "0" * 64
        db.session.commit()
        with patch("app.views.api.get_storage_backend") as mock_storage:
            backend = MagicMock()
            backend.get.return_value = b"different-content"
            mock_storage.return_value = backend
            resp = client.get(f"/api/v1/vouchers/{v.id}/verify",
                              headers=auth_header)
        body = resp.get_json()
        assert body["verified"] is False


class TestVoucherLogs:
    def test_no_auth(self, client):
        resp = client.get("/api/v1/vouchers/1/logs")
        assert resp.status_code == 401

    def test_404(self, client, auth_header, accounts):
        resp = client.get("/api/v1/vouchers/9999/logs",
                          headers=auth_header)
        assert resp.status_code == 404

    def test_returns_logs(self, db, client, user, auth_header, accounts):
        v = make_voucher(db, user.id)
        db.session.add(VoucherAuditLog(
            voucher_id=v.id, user_id=user.id, action="orphaned",
        ))
        db.session.add(VoucherAuditLog(
            voucher_id=v.id, user_id=user.id, action="hash_verified",
        ))
        db.session.commit()
        resp = client.get(f"/api/v1/vouchers/{v.id}/logs",
                          headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body["logs"]) == 2
