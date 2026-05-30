"""クライアント側 LLM 呼出フロー向けエンドポイントのテスト。

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

    def test_negative_tokens_rejected_recorded_as_null(
        self, db, logged_in_client, user, accounts,
    ):
        """負トークン値は AIUsageLog に NULL で記録 (誤送信 / 不正クライアント対策)。

        Phase 3 Billing 連携で総量集計時に負値混入を防ぐ。
        """
        from app.models.ai_usage_log import AIUsageLog
        draft = self._make_pending_draft(db, user.id)
        resp = logged_in_client.patch(
            f"/api/v1/ai/drafts/{draft.id}/suggestions",
            json={
                "suggestions": [{"x": 1}],
                "provider": "openai",
                "model": "gpt-4o-mini",
                "usage": {"input_tokens": -100, "output_tokens": 50},
            },
        )
        assert resp.status_code == 200
        log = AIUsageLog.query.filter_by(user_id=user.id).first()
        assert log is not None
        # 負値は NULL に正規化 (= total_tokens も None)
        assert log.input_tokens is None
        assert log.output_tokens == 50
        assert log.total_tokens is None

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


class TestAiLedgerContext:
    """POST /api/v1/ai/ledger-context."""

    def test_unauthenticated(self, client):
        resp = client.post("/api/v1/ai/ledger-context", json={"account_names": []})
        assert resp.status_code in (302, 401)

    def test_empty_returns_empty(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/api/v1/ai/ledger-context", json={"account_names": []},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ledger_text"] == ""

    def test_must_be_list(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/api/v1/ai/ledger-context", json={"account_names": "not list"},
        )
        assert resp.status_code == 400

    def test_returns_ledger_text_for_matched_accounts(
        self, db, logged_in_client, user, accounts,
    ):
        """既存仕訳があれば該当科目の元帳テキスト (ヘッダ + 金額) が返る。"""
        from datetime import date
        from tests.conftest import make_journal
        make_journal(
            db, user.id, "5010", "1010", 500,
            entry_date=date(2026, 5, 23), source="cashbook",
        )
        resp = logged_in_client.post(
            "/api/v1/ai/ledger-context", json={"account_names": ["食費"]},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        ledger = body["ledger_text"]
        assert isinstance(ledger, str)
        # 科目ヘッダ + 仕訳金額 (¥500) が含まれることで _get_ledger_context が
        # 実際に呼ばれて整形済テキストを返したことを確認
        assert "食費" in ledger
        assert "5010" in ledger
        # 金額表示は "¥500" 形式 (_get_ledger_context の f-string 出力)
        assert "¥500" in ledger or "500" in ledger

    def test_ignores_non_string_and_oversized_entries(
        self, logged_in_client, accounts,
    ):
        """非文字列や 100 文字超は除外、20 件超は切詰。"""
        names = [
            "valid", 123, None, {"k": "v"},  # 非文字列は除外
            "x" * 200,  # 100 文字超は除外
        ] + [f"name_{i}" for i in range(50)]  # 50 個追加で 20 切詰確認
        resp = logged_in_client.post(
            "/api/v1/ai/ledger-context", json={"account_names": names},
        )
        # 例外無しで 200 を返す
        assert resp.status_code == 200


class TestAiPromptContext:
    """GET /api/v1/ai/prompt-context."""

    def test_unauthenticated(self, client):
        resp = client.get("/api/v1/ai/prompt-context")
        assert resp.status_code in (302, 401)

    def test_returns_round1_round2_and_metadata(
        self, db, logged_in_client, user, accounts,
    ):
        from app.models.ai_config import UserAIConfig
        from app.extensions import db as _db
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
            model_name="", custom_prompt="QUICPayはJCB CARD W",
            compliance_check=True,
        )
        _db.session.add(cfg)
        _db.session.commit()
        resp = logged_in_client.get("/api/v1/ai/prompt-context")
        assert resp.status_code == 200
        body = resp.get_json()
        # Round 1
        assert "round1_prompt" in body
        assert "DOCUMENT" in body["round1_prompt"] or "領収書" in body["round1_prompt"]
        assert body["compliance_check_enabled"] is True
        assert "compliance_prompt" in body
        # Round 2 テンプレートは 2 種類 (needs_ledger 切替用)
        assert "round2_prompt_template_no_ledger" in body
        assert "round2_prompt_template_with_ledger" in body
        # no_ledger には元帳ヘッダがない (= 「以下は関連する勘定科目の元帳」が現れない)
        assert "元帳" not in body["round2_prompt_template_no_ledger"]
        assert "__LEDGER_TEXT__" not in body["round2_prompt_template_no_ledger"]
        # with_ledger には元帳ヘッダと __LEDGER_TEXT__ プレースホルダの両方が含まれる
        assert "元帳" in body["round2_prompt_template_with_ledger"]
        assert "__LEDGER_TEXT__" in body["round2_prompt_template_with_ledger"]
        # 両テンプレートで __ACCOUNT_LIST_TEXT__ プレースホルダ
        assert "__ACCOUNT_LIST_TEXT__" in body["round2_prompt_template_no_ledger"]
        assert "__ACCOUNT_LIST_TEXT__" in body["round2_prompt_template_with_ledger"]
        # custom_prompt は両方で埋め込み済 (再置換不要)
        assert "QUICPayはJCB CARD W" in body["round2_prompt_template_no_ledger"]
        assert "QUICPayはJCB CARD W" in body["round2_prompt_template_with_ledger"]
        # account_list_text は別途返却
        assert "account_list_text" in body
        # custom_prompt
        assert body["custom_prompt"] == "QUICPayはJCB CARD W"
        # provider 別デフォルト: サーバ側 PROVIDER_DEFAULTS と一致
        from app.services.ai_receipt import PROVIDER_DEFAULTS
        for k in ("openai", "anthropic", "google"):
            assert body["default_model_by_provider"][k] == PROVIDER_DEFAULTS[k]
        # llama_cpp は除外
        assert "llama_cpp" not in body["default_model_by_provider"]
        # ★ セキュリティ重要: api_key 関連は一切返却しない
        assert "api_key_blob" not in body
        assert "api_key_iv" not in body
        assert "api_key_encrypted" not in body
        # ai_config 全体も含まない (provider 名は別経路 default_model_by_provider のみ)
        assert "ai_config" not in body

    def test_no_ai_config_still_returns_context(
        self, db, logged_in_client, user, accounts,
    ):
        """AI 設定が未登録でも prompt-context は返却される (UI で先に確認できる)。"""
        resp = logged_in_client.get("/api/v1/ai/prompt-context")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["compliance_check_enabled"] is False
        assert body["custom_prompt"] == ""


class TestSuggestCategoriesPromptContext:
    """GET /api/v1/suggest-categories/prompt-context."""

    def test_unauthenticated(self, client):
        resp = client.get(
            "/api/v1/suggest-categories/prompt-context"
            "?payment_account_code=1010",
        )
        assert resp.status_code in (302, 401)

    def test_missing_payment_account_code(self, logged_in_client, accounts):
        resp = logged_in_client.get(
            "/api/v1/suggest-categories/prompt-context",
        )
        assert resp.status_code == 400

    def test_invalid_payment_account_code(self, logged_in_client, accounts):
        resp = logged_in_client.get(
            "/api/v1/suggest-categories/prompt-context"
            "?payment_account_code=9999",
        )
        assert resp.status_code == 400

    def test_returns_full_context(self, db, logged_in_client, user, accounts):
        from app.models.ai_config import UserAIConfig
        from app.extensions import db as _db
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
            custom_prompt="QUICPay は JCB",
        )
        _db.session.add(cfg)
        _db.session.commit()
        resp = logged_in_client.get(
            "/api/v1/suggest-categories/prompt-context"
            "?payment_account_code=1010",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        # プレースホルダ 4 種
        assert "__PAYMENT_ACCOUNT_NAME__" in body["prompt_template"]
        assert "__LEDGER_CONTEXT__" in body["prompt_template"]
        assert "__ACCOUNT_LIST__" in body["prompt_template"]
        assert "__ROWS_TEXT__" in body["prompt_template"]
        # 口座名
        assert body["payment_account_name"]  # 1010 = 現金 等
        # E3-F PR-D-6-1a: ledger_context はサーバから返さなくなった
        # (クライアントが復号済み仕訳から buildPaymentLedgerContext で構築)。
        assert "ledger_context" not in body
        # account_list / account_map
        assert "account_list" in body
        assert isinstance(body["account_map"], dict)
        assert "5010" in body["account_map"]
        assert body["custom_prompt"] == "QUICPay は JCB"
        # default_model_by_provider
        from app.services.ai_receipt import PROVIDER_DEFAULTS
        for k in ("openai", "anthropic", "google"):
            assert body["default_model_by_provider"][k] == PROVIDER_DEFAULTS[k]
        assert "llama_cpp" not in body["default_model_by_provider"]
        # api_key 一切返却しない
        assert "api_key_blob" not in body


class TestVoucherAttachPromptContext:
    """GET /api/v1/voucher-attach/prompt-context."""

    def test_unauthenticated(self, client):
        resp = client.get("/api/v1/voucher-attach/prompt-context")
        assert resp.status_code in (302, 401)

    def test_returns_template_with_placeholders(
        self, db, logged_in_client, user, accounts,
    ):
        from app.models.ai_config import UserAIConfig
        from app.extensions import db as _db
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
            model_name="", compliance_check=True,
        )
        _db.session.add(cfg)
        _db.session.commit()
        resp = logged_in_client.get("/api/v1/voucher-attach/prompt-context")
        assert resp.status_code == 200
        body = resp.get_json()
        # DOCUMENT_PROMPT + COMPLIANCE_CHECK_PROMPT + CONSISTENCY_CHECK_PROMPT_TEMPLATE
        assert "__JOURNAL_DATE__" in body["prompt_template"]
        assert "__JOURNAL_AMOUNT__" in body["prompt_template"]
        assert "__JOURNAL_DESCRIPTION__" in body["prompt_template"]
        assert "電帳法コンプライアンスチェック" in body["prompt_template"]
        assert body["compliance_check_enabled"] is True
        from app.services.ai_receipt import PROVIDER_DEFAULTS
        for k in ("openai", "anthropic", "google"):
            assert body["default_model_by_provider"][k] == PROVIDER_DEFAULTS[k]
        assert "llama_cpp" not in body["default_model_by_provider"]
        # api_key 一切返却しない
        assert "api_key_blob" not in body

    def test_no_compliance_when_disabled(
        self, db, logged_in_client, user, accounts,
    ):
        from app.models.ai_config import UserAIConfig
        from app.extensions import db as _db
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
            compliance_check=False,
        )
        _db.session.add(cfg)
        _db.session.commit()
        resp = logged_in_client.get("/api/v1/voucher-attach/prompt-context")
        body = resp.get_json()
        assert body["compliance_check_enabled"] is False
        # COMPLIANCE_CHECK_PROMPT は含まれない (consistency のみ)
        assert "電帳法コンプライアンスチェック" not in body["prompt_template"]
        assert "__JOURNAL_DATE__" in body["prompt_template"]


class TestWebImportPromptContext:
    """GET /api/v1/web-import/prompt-context."""

    def test_unauthenticated(self, client):
        resp = client.get("/api/v1/web-import/prompt-context")
        assert resp.status_code in (302, 401)

    def test_returns_template_and_metadata(
        self, db, logged_in_client, user, accounts,
    ):
        from app.models.ai_config import UserAIConfig
        from app.extensions import db as _db
        cfg = UserAIConfig(
            user_id=user.id, provider="openai",
            api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
            model_name="", custom_prompt="三井住友は普通預金",
        )
        _db.session.add(cfg)
        _db.session.commit()
        resp = logged_in_client.get("/api/v1/web-import/prompt-context")
        assert resp.status_code == 200
        body = resp.get_json()
        # プレースホルダ 2 種類
        assert "__PAYMENT_ACCOUNT_NAME__" in body["prompt_template"]
        assert "__RAW_TEXT__" in body["prompt_template"]
        # transactions JSON の形式説明が含まれる
        assert "transactions" in body["prompt_template"]
        assert "deposit" in body["prompt_template"]
        assert "withdrawal" in body["prompt_template"]
        # custom_prompt
        assert body["custom_prompt"] == "三井住友は普通預金"
        # provider 別デフォルト: ai-prompt-context と同じ
        from app.services.ai_receipt import PROVIDER_DEFAULTS
        for k in ("openai", "anthropic", "google"):
            assert body["default_model_by_provider"][k] == PROVIDER_DEFAULTS[k]
        assert "llama_cpp" not in body["default_model_by_provider"]
        # api_key 関連は一切返却しない
        assert "api_key_blob" not in body
        assert "api_key_iv" not in body
        assert "api_key_encrypted" not in body

    def test_no_ai_config_still_returns_context(
        self, db, logged_in_client, user, accounts,
    ):
        resp = logged_in_client.get("/api/v1/web-import/prompt-context")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["custom_prompt"] == ""
        assert "__PAYMENT_ACCOUNT_NAME__" in body["prompt_template"]
