"""E2 PR-C-2: クライアント側 LLM 呼出フロー向けエンドポイントのテスト。

対象:
  POST  /api/v1/ai/uploads                       — 画像アップロード (LLM 呼出なし)
  PATCH /api/v1/ai/drafts/<id>/suggestions       — クライアント解析結果の保存
"""

import io
import json

from app.extensions import db
from app.models.ai_draft import AIDraft


def _image_bytes(n=64):
    """テスト用 PNG-like なバイト列 (mime チェック通過のため PNG signature 付き)。"""
    png_sig = b"\x89PNG\r\n\x1a\n"
    return png_sig + b"\x00" * (n - len(png_sig))


def _upload(client, *, comment=None, image_name="r.png"):
    data = {
        "image": (io.BytesIO(_image_bytes()), image_name, "image/png"),
    }
    if comment is not None:
        data["comment"] = comment
    return client.post(
        "/api/v1/ai/uploads",
        data=data,
        content_type="multipart/form-data",
    )


class TestAiUpload:
    def test_unauthenticated(self, client):
        resp = client.post("/api/v1/ai/uploads")
        assert resp.status_code in (302, 401)

    def test_success_creates_pending_draft(
        self, db, logged_in_client, user, accounts,
    ):
        resp = _upload(logged_in_client, comment="メモ")
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["ok"] is True
        assert "draft_id" in body
        assert body["status"] == "pending"
        # AIDraft レコード確認 (LLM 呼出は走っていないので suggestions は空)
        draft = AIDraft.query.get(body["draft_id"])
        assert draft is not None
        assert draft.status == "pending"
        assert draft.comment == "メモ"
        assert json.loads(draft.suggestions_json) == []
        assert draft.image_key  # 画像はストレージに保存されている

    def test_missing_image(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/api/v1/ai/uploads",
            data={},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "image" in resp.get_json()["error"]

    def test_unsupported_mime_type(self, logged_in_client, accounts):
        data = {
            "image": (io.BytesIO(b"GIF89a..."), "x.svg", "image/svg+xml"),
        }
        resp = logged_in_client.post(
            "/api/v1/ai/uploads",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "対応していない" in resp.get_json()["error"]


class TestAiSaveSuggestions:
    def _make_pending_draft(self, db, user_id):
        from app.services.storage import make_storage_key
        draft = AIDraft(
            user_id=user_id, image_key="dummy/key",
            image_mime="image/png",
            file_hash="0" * 64, file_size=100,
            suggestions_json="[]", status="pending",
        )
        db.session.add(draft)
        db.session.commit()
        return draft

    def test_unauthenticated(self, client):
        resp = client.patch("/api/v1/ai/drafts/1/suggestions")
        assert resp.status_code in (302, 401)

    def test_not_found(self, logged_in_client, accounts):
        resp = logged_in_client.patch(
            "/api/v1/ai/drafts/99999/suggestions",
            json={"suggestions": []},
        )
        assert resp.status_code == 404

    def test_idor_other_user(
        self, db, app, client, user, auditor, accounts,
    ):
        """別ユーザーの draft に suggestions を書き込めない。"""
        other_draft = self._make_pending_draft(db, auditor.id)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
        resp = client.patch(
            f"/api/v1/ai/drafts/{other_draft.id}/suggestions",
            json={"suggestions": [{"title": "x"}]},
        )
        # 404 で IDOR 隠蔽 (自分の draft でないなら「存在しない」と返す)
        assert resp.status_code == 404
        db.session.refresh(other_draft)
        # 他人の draft は変更されない
        assert other_draft.status == "pending"
        assert json.loads(other_draft.suggestions_json) == []

    def test_success_updates_to_analyzed(
        self, db, logged_in_client, user, accounts,
    ):
        draft = self._make_pending_draft(db, user.id)
        suggestions = [{
            "title": "セブン-イレブン",
            "date": "2026-05-23",
            "entry_description": "コーヒー",
            "lines": [
                {"account_code": "5010", "debit_amount": 500, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 500},
            ],
        }]
        resp = logged_in_client.patch(
            f"/api/v1/ai/drafts/{draft.id}/suggestions",
            json={
                "suggestions": suggestions,
                "usage": {"input_tokens": 100, "output_tokens": 30},
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["draft"]["id"] == draft.id
        # DB 更新確認
        db.session.refresh(draft)
        assert draft.status == "analyzed"
        assert json.loads(draft.suggestions_json) == suggestions

    def test_suggestions_must_be_list(
        self, db, logged_in_client, user, accounts,
    ):
        draft = self._make_pending_draft(db, user.id)
        resp = logged_in_client.patch(
            f"/api/v1/ai/drafts/{draft.id}/suggestions",
            json={"suggestions": "not a list"},
        )
        assert resp.status_code == 400
        assert "list" in resp.get_json()["error"]

    def test_suggestions_too_large(
        self, db, logged_in_client, user, accounts,
    ):
        draft = self._make_pending_draft(db, user.id)
        # 200KB 超えの suggestions
        big = [{"k": "x" * 250000}]
        resp = logged_in_client.patch(
            f"/api/v1/ai/drafts/{draft.id}/suggestions",
            json={"suggestions": big},
        )
        assert resp.status_code == 413
        assert "too large" in resp.get_json()["error"]

    def test_done_status_cannot_be_overwritten(
        self, db, logged_in_client, user, accounts,
    ):
        """done (= 仕訳登録済み) を二度上書きさせない。"""
        draft = self._make_pending_draft(db, user.id)
        draft.status = "done"
        db.session.commit()
        resp = logged_in_client.patch(
            f"/api/v1/ai/drafts/{draft.id}/suggestions",
            json={"suggestions": [{"x": 1}]},
        )
        assert resp.status_code == 400

    def test_suggestions_byte_size_check_utf8(
        self, db, logged_in_client, user, accounts,
    ):
        """日本語 (3 byte/char) で文字数は上限内でもバイト数で reject されること。

        旧コード (len(str)) では日本語 200K 文字 = 600K bytes 通過していた。
        修正後は UTF-8 バイト数で判定する (PR #150 review NG-1)。
        """
        draft = self._make_pending_draft(db, user.id)
        # 全角 100K 文字 ≒ 300K bytes (UTF-8) → 200KB 超過で 413 となる
        big_jp = [{"description": "あ" * 100000}]
        resp = logged_in_client.patch(
            f"/api/v1/ai/drafts/{draft.id}/suggestions",
            json={"suggestions": big_jp},
        )
        assert resp.status_code == 413
        assert "too large" in resp.get_json()["error"]

    def test_logs_usage_when_provider_and_model_provided(
        self, db, logged_in_client, user, accounts,
    ):
        """provider/model/usage を受け取った場合に AIUsageLog レコードを作成する。

        PR #150 review NG-2: クライアント LLM の利用量がサーバ側で記録されて
        いなかった問題を解消。
        """
        from app.models.ai_usage_log import AIUsageLog
        draft = self._make_pending_draft(db, user.id)
        resp = logged_in_client.patch(
            f"/api/v1/ai/drafts/{draft.id}/suggestions",
            json={
                "suggestions": [{"title": "x"}],
                "provider": "openai",
                "model": "gpt-4o-mini",
                "usage": {"input_tokens": 100, "output_tokens": 30},
            },
        )
        assert resp.status_code == 200
        log = AIUsageLog.query.filter_by(user_id=user.id).first()
        assert log is not None
        assert log.provider == "openai"
        assert log.model == "gpt-4o-mini"
        assert log.input_tokens == 100
        assert log.output_tokens == 30
        assert log.total_tokens == 130
        assert log.feature == "receipt_client_side"
        assert log.status == "ok"

    def test_no_usage_log_without_provider(
        self, db, logged_in_client, user, accounts,
    ):
        """provider/model 未指定なら AIUsageLog 記録しない (後方互換)。"""
        from app.models.ai_usage_log import AIUsageLog
        draft = self._make_pending_draft(db, user.id)
        resp = logged_in_client.patch(
            f"/api/v1/ai/drafts/{draft.id}/suggestions",
            json={"suggestions": [{"x": 1}]},
        )
        assert resp.status_code == 200
        assert AIUsageLog.query.filter_by(user_id=user.id).count() == 0

    def test_can_overwrite_analyzed(
        self, db, logged_in_client, user, accounts,
    ):
        """analyzed (LLM 一度走った) は再上書き可能 (UI 編集 / 再実行)。"""
        draft = self._make_pending_draft(db, user.id)
        draft.status = "analyzed"
        draft.suggestions_json = json.dumps([{"old": 1}])
        db.session.commit()
        resp = logged_in_client.patch(
            f"/api/v1/ai/drafts/{draft.id}/suggestions",
            json={"suggestions": [{"new": 2}]},
        )
        assert resp.status_code == 200
        db.session.refresh(draft)
        assert json.loads(draft.suggestions_json) == [{"new": 2}]
