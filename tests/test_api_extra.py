"""REST API (api.py) の追加テスト

既存 test_api.py は journals 系を中心。こちらは ai/analyze, ai/drafts,
vouchers エンドポイントをカバー。
"""

import io
from datetime import date
from unittest.mock import MagicMock, patch

from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.journal import JournalEntry
from app.models.voucher import Voucher
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
                    entry_description="x", lines=[],
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


# =====================================================================
# 以下はカバレッジ改善タスクで追加したテスト (既存テストとの重複回避)
# =====================================================================


class TestCreateJournalExtra:
    def test_empty_body(self, logged_in_client, auth_header, user, accounts):
        resp = logged_in_client.post("/api/v1/journals", headers=auth_header, json={})
        assert resp.status_code == 400
        assert "JSON ボディ" in resp.get_json()["error"]

    def test_line_without_account_code(
        self, logged_in_client, auth_header, user, accounts
    ):
        resp = logged_in_client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"debit": 100, "credit": 0},
                {"account_code": "1010", "debit": 0, "credit": 100},
            ],
        })
        assert resp.status_code == 400
        assert "account_code" in resp.get_json()["error"]

    def test_submitted_account_locked(
        self, db, logged_in_client, auth_header, user, accounts, auditor
    ):
        """提出済み公開科目 (5010) を使う起票は拒否される"""
        from app.models.audit import AuditGrant, AuditGrantAccount
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="submitted",
        )
        db.session.add(grant)
        db.session.flush()
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id, account_user_id=user.id,
            account_code="5010",
        ))
        db.session.commit()
        resp = logged_in_client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": "5010", "debit": 100, "credit": 0},
                {"account_code": "1010", "debit": 0, "credit": 100},
            ],
        })
        assert resp.status_code == 400
        assert "提出済み" in resp.get_json()["error"]

    def test_with_draft_id_success(
        self, db, logged_in_client, auth_header, user, accounts
    ):
        """draft_id 付き起票で下書き → 証憑移行 (create_voucher_from_draft) される"""
        draft = AIDraft(
            user_id=user.id, image_key="drafts/1/x.jpg",
            image_mime="image/jpeg", file_hash="a" * 64, file_size=100,
            status="analyzed",
            suggestions_json='[{"title": "t"}]',
        )
        db.session.add(draft)
        db.session.commit()
        with patch("app.views.api.create_voucher_from_draft") as mock_v:
            resp = logged_in_client.post("/api/v1/journals", headers=auth_header, json={
                "date": "2026-02-15", "description": "x",
                "lines": [
                    {"account_code": "5010", "debit": 100, "credit": 0},
                    {"account_code": "1010", "debit": 0, "credit": 100},
                ],
                "draft_id": draft.id,
            })
        assert resp.status_code == 201
        mock_v.assert_called_once()


class TestListJournalsExtra:
    def test_invalid_date_from(self, logged_in_client, auth_header, accounts):
        resp = logged_in_client.get("/api/v1/journals?date_from=2026-13-99",
                                    headers=auth_header)
        assert resp.status_code == 400

    def test_invalid_date_to(self, logged_in_client, auth_header, accounts):
        resp = logged_in_client.get("/api/v1/journals?date_to=not-a-date",
                                    headers=auth_header)
        assert resp.status_code == 400


class TestDeleteJournalExtra:
    def test_submitted_account_locked(
        self, db, logged_in_client, auth_header, user, accounts, auditor
    ):
        from app.models.audit import AuditGrant, AuditGrantAccount
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15), source="journal")
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="submitted",
        )
        db.session.add(grant)
        db.session.flush()
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id, account_user_id=user.id,
            account_code="5010",
        ))
        db.session.commit()
        resp = logged_in_client.delete(f"/api/v1/journals/{entry.id}",
                                       headers=auth_header)
        assert resp.status_code == 400
        assert "提出済み" in resp.get_json()["error"]
        assert db.session.get(JournalEntry, entry.id) is not None


class TestOAuthTokenAuth:
    def _make_oauth_token(self, db, user, raw="ikt_testtoken1234567890", read_only=False):
        from app.models.oauth import OAuthToken
        token = OAuthToken(
            user_id=user.id, name="test",
            token_hash=OAuthToken.hash_token(raw),
            token_prefix="ikt_tes...", is_active=True,
            read_only=read_only,
        )
        db.session.add(token)
        db.session.commit()
        return raw

    def test_oauth_token_accepted(self, db, logged_in_client, user, accounts):
        raw = self._make_oauth_token(db, user)
        resp = logged_in_client.get("/api/v1/journals",
                                    headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_invalid_oauth_token(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.get("/api/v1/journals",
                                    headers={"Authorization": "Bearer ikt_invalid"})
        assert resp.status_code == 401

    def test_read_only_token_rejected_for_write(
        self, db, logged_in_client, user, accounts
    ):
        raw = self._make_oauth_token(db, user, read_only=True)
        resp = logged_in_client.post("/api/v1/journals", json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": "5010", "debit": 100, "credit": 0},
                {"account_code": "1010", "debit": 0, "credit": 100},
            ],
        }, headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 403
        assert "読み取り専用" in resp.get_json()["error"]


class TestAiAnalyzeExtra:
    def test_quota_exceeded(self, db, logged_in_client, auth_header, user, accounts):
        """QuotaExceededError は 413 で返る"""
        from app.services.storage_quota import QuotaExceededError
        _setup_ai_config(db, user.id)
        with patch("app.views.api.check_quota",
                   side_effect=QuotaExceededError("容量上限を超えます。")):
            resp = logged_in_client.post(
                "/api/v1/ai/analyze",
                headers=auth_header,
                data={"image": (io.BytesIO(_png_bytes()), "x.png", "image/png")},
                content_type="multipart/form-data",
            )
        assert resp.status_code == 413
        assert "容量上限" in resp.get_json()["error"]

    def test_success_with_notify(self, db, logged_in_client, auth_header, user, accounts):
        """notify=1 で Webhook 通知が送られる"""
        from app.services.ai_receipt import JournalSuggestion
        _setup_ai_config(db, user.id)
        with patch("app.services.ai_receipt.analyze_and_suggest") as mock_a, \
             patch("app.services.storage.store_image_with_thumbnail"), \
             patch("app.views.api._send_draft_notification") as mock_notify:
            mock_a.return_value = [
                JournalSuggestion(
                    title="x", description="", date="2026-02-15",
                    entry_description="x", lines=[],
                ),
            ]
            resp = logged_in_client.post(
                "/api/v1/ai/analyze",
                headers=auth_header,
                data={
                    "image": (io.BytesIO(_png_bytes()), "x.png", "image/png"),
                    "notify": "1",
                },
                content_type="multipart/form-data",
            )
        assert resp.status_code == 201
        mock_notify.assert_called_once()

    def test_rollback_when_over_quota_after_upload(
        self, db, logged_in_client, auth_header, user, accounts, monkeypatch,
    ):
        """アップロード後の楽観的再検証で超過が判明したら下書きをロールバック"""
        from app.models.storage import StorageUsage
        from app.services.ai_receipt import JournalSuggestion
        from app.views import api as api_module

        _setup_ai_config(db, user.id)
        monkeypatch.setattr(api_module, "is_over_quota", lambda owner: True)
        with patch("app.services.ai_receipt.analyze_and_suggest") as mock_a, \
             patch("app.services.storage.store_image_with_thumbnail"):
            mock_a.return_value = [
                JournalSuggestion(
                    title="x", description="", date="2026-02-15",
                    entry_description="x", lines=[],
                ),
            ]
            resp = logged_in_client.post(
                "/api/v1/ai/analyze",
                headers=auth_header,
                data={"image": (io.BytesIO(_png_bytes()), "x.png", "image/png")},
                content_type="multipart/form-data",
            )
        assert resp.status_code == 413
        assert AIDraft.query.filter_by(user_id=user.id).count() == 0
        usage = db.session.get(StorageUsage, user.id)
        assert usage is None or usage.used_bytes == 0


class TestAiDraftsExtra:
    def test_detail_with_suggestions(self, db, logged_in_client, auth_header, user, accounts):
        """詳細 API はサマリ + 候補リストを返す"""
        draft = AIDraft(
            user_id=user.id, image_key="d.jpg", image_mime="image/jpeg",
            status="analyzed",
            suggestions_json=(
                '[{"title": "食費", "date": "2026-02-15", '
                '"entry_description": "セブン", '
                '"lines": [{"debit_amount": 500}]}]'
            ),
        )
        db.session.add(draft)
        db.session.commit()
        resp = logged_in_client.get(f"/api/v1/ai/drafts/{draft.id}",
                                    headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()["draft"]
        assert body["summary"]["amount"] == 500
        assert body["summary"]["suggestion_count"] == 1
        assert len(body["suggestions"]) == 1

    def test_delete_releases_quota(
        self, db, logged_in_client, auth_header, user, accounts
    ):
        """下書き削除で StorageUsage が減算される"""
        from app.models.storage import StorageUsage
        db.session.add(StorageUsage(user_id=user.id, used_bytes=1000))
        draft = AIDraft(
            user_id=user.id, image_key="del.jpg", image_mime="image/jpeg",
            status="analyzed", file_size=300,
        )
        db.session.add(draft)
        db.session.commit()
        draft_id = draft.id
        with patch("app.views.api.get_storage_backend") as mock_b:
            backend = mock_b.return_value
            resp = logged_in_client.delete(f"/api/v1/ai/drafts/{draft_id}",
                                           headers=auth_header)
        assert resp.status_code == 200
        backend.delete.assert_called()
        usage = db.session.get(StorageUsage, user.id)
        assert usage.used_bytes == 700


class TestVouchersApiExtra:
    def test_invalid_date_to(self, logged_in_client, auth_header, accounts):
        resp = logged_in_client.get("/api/v1/vouchers?date_to=bad",
                                    headers=auth_header)
        assert resp.status_code == 400

    def test_amount_to_filter(self, db, logged_in_client, auth_header, user, accounts):
        """amount_to (金額上限) フィルタ"""
        e1 = make_journal(db, user.id, "5010", "1010", 100,
                          entry_date=date(2026, 2, 15), source="ai_receipt")
        e2 = make_journal(db, user.id, "5010", "1010", 5000,
                          entry_date=date(2026, 2, 16), source="ai_receipt")
        make_voucher(db, user.id, journal_entry_id=e1.id, image_key="vouchers/1/1.jpg")
        make_voucher(db, user.id, journal_entry_id=e2.id, image_key="vouchers/1/2.jpg")
        resp = logged_in_client.get("/api/v1/vouchers?amount_to=1000",
                                    headers=auth_header)
        body = resp.get_json()
        assert body["total"] == 1

    def test_image_missing_file(self, db, logged_in_client, auth_header, user, accounts):
        """証憑は存在するが画像ファイルが消えている場合は 404"""
        v = make_voucher(db, user.id, image_key="vouchers/1/missing.jpg")
        resp = logged_in_client.get(f"/api/v1/vouchers/{v.id}/image",
                                    headers=auth_header)
        assert resp.status_code == 404


class TestReportsApi:
    def test_trial_balance(self, db, logged_in_client, auth_header, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="journal")
        resp = logged_in_client.get(
            "/api/v1/reports/trial-balance?year=2026", headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert any(b["account_code"] == "5010" for b in body["balances"])

    def test_income_statement(self, db, logged_in_client, auth_header, user, accounts):
        make_journal(db, user.id, "1010", "4010", 300000,
                     entry_date=date(2026, 2, 15), source="journal")
        resp = logged_in_client.get(
            "/api/v1/reports/income-statement?year=2026&month=2",
            headers=auth_header)
        assert resp.status_code == 200
        assert resp.get_json()["month"] == 2

    def test_income_statement_invalid_month(self, logged_in_client, auth_header, accounts):
        resp = logged_in_client.get(
            "/api/v1/reports/income-statement?month=13", headers=auth_header)
        assert resp.status_code == 400

    def test_monthly_comparison(self, db, logged_in_client, auth_header, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="journal")
        resp = logged_in_client.get("/api/v1/reports/monthly?year=2026",
                                    headers=auth_header)
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_tax_summary(self, db, logged_in_client, auth_header, user, accounts):
        # tax_category 付き科目 (社会保険料控除) で仕訳を作る
        from app.models.account import Account
        acct = Account(
            user_id=user.id, account_type_id=accounts["5010"].account_type_id,
            code="5011", name="社会保険料",
            tax_category="social_insurance", is_active=True,
        )
        db.session.add(acct)
        db.session.commit()
        make_journal(db, user.id, "5011", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="journal")
        resp = logged_in_client.get("/api/v1/reports/tax?year=2026",
                                    headers=auth_header)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["tax_summary"]["social_insurance"]["total"] == 1000
