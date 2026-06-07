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
    """AI 設定を雛形で作る"""
    from app.services.ai_receipt import encrypt_api_key
    cfg = UserAIConfig(
        user_id=user_id, provider="openai",
        api_key_encrypted=encrypt_api_key("k"),
        model_name="gpt-4",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


class TestAiAnalyze:
    def test_no_auth(self, client):
        resp = client.post("/api/v1/ai/analyze")
        assert resp.status_code == 401

    def test_no_ai_config(self, client, auth_header, accounts):
        # AI 設定なし
        resp = client.post("/api/v1/ai/analyze",
                           headers=auth_header,
                           data={}, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_no_image(self, db, client, user, auth_header, accounts):
        _setup_ai_config(db, user.id)
        resp = client.post("/api/v1/ai/analyze",
                           headers=auth_header,
                           data={}, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "image" in resp.get_json()["error"]

    def test_oversized(self, db, client, user, auth_header, accounts):
        _setup_ai_config(db, user.id)
        big = b"\x89PNG" + b"\x00" * (11 * 1024 * 1024)  # 11MB
        resp = client.post("/api/v1/ai/analyze",
                           headers=auth_header,
                           data={"image": (io.BytesIO(big), "big.png")},
                           content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_unsupported_mime(self, db, client, user, auth_header, accounts):
        _setup_ai_config(db, user.id)
        resp = client.post(
            "/api/v1/ai/analyze",
            headers=auth_header,
            data={"image": (io.BytesIO(b"not-image"), "x.txt", "text/plain")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_success(self, db, client, user, auth_header, accounts):
        _setup_ai_config(db, user.id)
        with patch("app.services.ai_receipt.analyze_and_suggest") as mock_a, \
             patch("app.services.storage.store_image_with_thumbnail"):
            from app.services.ai_receipt import JournalSuggestion
            mock_a.return_value = [
                JournalSuggestion(
                    title="領収書", description="セブン",
                    date="2026-02-15",
                    entry_description="ファミマ",
                    lines=[
                        {"account_code": "5010", "debit_amount": 100,
                         "credit_amount": 0, "description": ""},
                        {"account_code": "1010", "debit_amount": 0,
                         "credit_amount": 100, "description": ""},
                    ],
                ),
            ]
            resp = client.post(
                "/api/v1/ai/analyze",
                headers=auth_header,
                data={
                    "image": (io.BytesIO(_png_bytes()), "x.png", "image/png"),
                    "comment": "テスト",
                },
                content_type="multipart/form-data",
            )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["ok"] is True
        assert "draft_id" in body

    def test_analyze_error(self, db, client, user, auth_header, accounts):
        _setup_ai_config(db, user.id)
        with patch("app.services.ai_receipt.analyze_and_suggest") as mock_a:
            mock_a.side_effect = ValueError("AI解析失敗")
            resp = client.post(
                "/api/v1/ai/analyze",
                headers=auth_header,
                data={
                    "image": (io.BytesIO(_png_bytes()), "x.png", "image/png"),
                },
                content_type="multipart/form-data",
            )
        assert resp.status_code == 400

    def test_record_upload_failure_does_not_record_delete(
        self, db, client, user, auth_header, accounts, monkeypatch,
    ):
        """api.create_draft: record_upload 失敗時に TOCTOU 検証スキップで
        record_delete が呼ばれない (他ユーザー計上の誤減算経路を遮断)。
        ai_journal.analyze 側の同等テストと対称に追加。
        """
        from app.models.storage import StorageUsage
        from app.services.ai_receipt import JournalSuggestion
        from app.views import api as api_module

        _setup_ai_config(db, user.id)
        # 上限近くまで埋まった他ユーザー相当の計上を作る (495MB)
        db.session.add(StorageUsage(user_id=user.id, used_bytes=495 * 1024 * 1024))
        db.session.commit()

        def fake_record_upload(*args, **kwargs):
            raise RuntimeError("DB connection lost")

        record_delete_called = []

        def fake_record_delete(*args, **kwargs):
            record_delete_called.append(args)

        monkeypatch.setattr(api_module, "record_upload", fake_record_upload)
        monkeypatch.setattr(api_module, "record_delete", fake_record_delete)

        with patch("app.services.ai_receipt.analyze_and_suggest") as mock_a, \
             patch("app.services.storage.store_image_with_thumbnail"):
            mock_a.return_value = [
                JournalSuggestion(
                    title="x", description="", date="2026-02-15",
                    entry_description="x", lines=[], compliance=None,
                ),
            ]
            resp = client.post(
                "/api/v1/ai/analyze",
                headers=auth_header,
                data={
                    "image": (io.BytesIO(_png_bytes()), "x.png", "image/png"),
                },
                content_type="multipart/form-data",
            )

        # 201 (Created) — Draft は永続化される
        assert resp.status_code == 201
        # record_delete は呼ばれていない (誤減算なし)
        assert record_delete_called == []
        # StorageUsage は変動なし (record_upload 失敗のため)
        usage = db.session.get(StorageUsage, user.id)
        assert usage.used_bytes == 495 * 1024 * 1024


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
