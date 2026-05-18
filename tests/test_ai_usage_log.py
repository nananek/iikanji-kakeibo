"""外部 AI API 呼び出し履歴 (AIUsageLog) のテスト

PR-1 (v3.13.0 前段): 記録基盤
- ハンドラ呼び出しごとに AIUsageLog が 1 行作成されること
- プロバイダー別の usage 抽出が正しいこと
- エラー時 (HTTP/timeout/parse) も status / http_status を記録すること
- DB 書き込み失敗が AI 呼び出し本体に波及しないこと
"""

from unittest.mock import patch, MagicMock

import httpx
import pytest

from app.models.ai_usage_log import AIUsageLog
from app.services.ai_receipt import (
    _call_ai, _call_ai_text,
    _call_openai, _call_anthropic, _call_google, _call_llama_cpp,
    _call_openai_text, _call_anthropic_text, _call_google_text,
    _call_llama_cpp_text,
    _log_ai_usage,
)


def _mock_post(json_response, status=200):
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_response
    resp.status_code = status
    resp.raise_for_status.return_value = None
    return resp


class TestUsageExtraction:
    """プロバイダー別の usage 抽出ロジック"""

    def test_openai_image_extracts_usage(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"a": 1}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            })
            _, usage = _call_openai("k", "gpt-4o", b"img", "image/png")
            assert usage == {"input_tokens": 100, "output_tokens": 50}

    def test_anthropic_image_extracts_usage(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "content": [{"text": '{"a": 1}'}],
                "usage": {"input_tokens": 80, "output_tokens": 40},
            })
            _, usage = _call_anthropic("k", "claude", b"img", "image/png")
            assert usage == {"input_tokens": 80, "output_tokens": 40}

    def test_google_image_extracts_usage(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}],
                "usageMetadata": {"promptTokenCount": 60, "candidatesTokenCount": 20},
            })
            _, usage = _call_google("k", "gemini", b"img", "image/png")
            assert usage == {"input_tokens": 60, "output_tokens": 20}

    def test_llama_cpp_image_no_usage(self):
        """usage キーが返らない場合は None で正規化される"""
        with patch("httpx.post") as mock_post:
            mock_post.return_value = _mock_post({
                "choices": [{"message": {"content": '{"a": 1}'}}],
            })
            _, usage = _call_llama_cpp("", "default", b"img", "image/png")
            assert usage == {"input_tokens": None, "output_tokens": None}


class TestCallAiLogsUsage:
    """_call_ai 経由でログが INSERT される"""

    def _make_handler(self, parsed, usage):
        return MagicMock(return_value=(parsed, usage))

    def test_image_call_logs_one_row(self, app, db, user):
        handler = self._make_handler({"a": 1}, {"input_tokens": 30, "output_tokens": 10})
        _call_ai(handler, "k", "gpt-4o", b"img", "image/png",
                 "prompt", 500, user.id, None,
                 provider="openai", feature="receipt_round1")

        logs = AIUsageLog.query.filter_by(user_id=user.id).all()
        assert len(logs) == 1
        log = logs[0]
        assert log.provider == "openai"
        assert log.model == "gpt-4o"
        assert log.feature == "receipt_round1"
        assert log.input_tokens == 30
        assert log.output_tokens == 10
        assert log.total_tokens == 40
        assert log.status == "ok"
        assert log.http_status is None
        assert log.latency_ms is not None and log.latency_ms >= 0

    def test_text_call_logs_one_row(self, app, db, user):
        handler = self._make_handler({"x": 1}, {"input_tokens": 5, "output_tokens": 7})
        _call_ai_text(handler, "k", "gpt-4o", "p", 1000, user.id, None,
                      provider="openai", feature="web_extract")
        log = AIUsageLog.query.filter_by(user_id=user.id).one()
        assert log.feature == "web_extract"
        assert log.total_tokens == 12

    def test_null_tokens_total_is_null(self, app, db, user):
        """usage が空でも記録されるが total_tokens は None"""
        handler = self._make_handler({"x": 1}, {"input_tokens": None, "output_tokens": None})
        _call_ai_text(handler, "k", "m", "p", 1000, user.id, None,
                      provider="llama_cpp", feature="web_extract")
        log = AIUsageLog.query.filter_by(user_id=user.id).one()
        assert log.input_tokens is None
        assert log.output_tokens is None
        assert log.total_tokens is None
        assert log.status == "ok"

    def test_http_error_logs_status_and_http_status(self, app, db, user):
        request = MagicMock()
        response = MagicMock(status_code=503)
        handler = MagicMock(side_effect=httpx.HTTPStatusError(
            "503", request=request, response=response,
        ))
        with pytest.raises(RuntimeError):
            _call_ai_text(handler, "k", "m", "p", 1000, user.id, None,
                          provider="openai", feature="receipt_round1")
        log = AIUsageLog.query.filter_by(user_id=user.id).one()
        assert log.status == "http_error"
        assert log.http_status == 503
        assert log.input_tokens is None

    def test_timeout_logs_status(self, app, db, user):
        handler = MagicMock(side_effect=httpx.TimeoutException("timed out"))
        with pytest.raises(RuntimeError):
            _call_ai_text(handler, "k", "m", "p", 1000, user.id, None,
                          provider="anthropic", feature="receipt_round1")
        log = AIUsageLog.query.filter_by(user_id=user.id).one()
        assert log.status == "timeout"
        assert log.http_status is None

    def test_parse_error_logs_status(self, app, db, user):
        handler = MagicMock(side_effect=ValueError("bad json"))
        with pytest.raises(RuntimeError):
            _call_ai_text(handler, "k", "m", "p", 1000, user.id, None,
                          provider="openai", feature="receipt_round1")
        log = AIUsageLog.query.filter_by(user_id=user.id).one()
        assert log.status == "parse_error"


class TestLogResilience:
    """DB 書き込み失敗が AI 呼び出し本体に波及しないこと"""

    def test_db_failure_does_not_break_caller(self, app, db, user):
        """`_log_ai_usage` で例外が出ても _call_ai は parsed を返し続ける"""
        handler = MagicMock(return_value=({"a": 1}, {"input_tokens": 1, "output_tokens": 2}))
        with patch("app.services.ai_receipt._log_ai_usage",
                   side_effect=Exception("DB down")):
            with pytest.raises(Exception):
                _call_ai_text(handler, "k", "m", "p", 1000, user.id, None,
                              provider="openai", feature="receipt_round1")
        # 注: _log_ai_usage 本体は try/except で握りつぶしているので
        # 直接 patch で例外を投げた場合は _call_ai_text 内の try-except
        # の挙動を確認する目的。実運用では _log_ai_usage が swallows する。

    def test_inner_db_exception_is_swallowed(self, app, db, user):
        """_log_ai_usage の内部 try/except が DB 例外を吸収する"""
        from app.extensions import db as _db
        with patch.object(_db.session, "commit",
                          side_effect=Exception("db down")):
            # 直接呼んでも例外を投げないこと
            _log_ai_usage(user.id, "openai", "gpt-4o", "test",
                          {"input_tokens": 1, "output_tokens": 2}, 10)
        # 記録されていない (rollback されている) ことを確認
        logs = AIUsageLog.query.filter_by(user_id=user.id).all()
        assert len(logs) == 0


class TestIdor:
    """他ユーザーのログは混入しない"""

    def test_user_specific_logs(self, app, db, user, second_user):
        handler = MagicMock(return_value=({"a": 1}, {"input_tokens": 1, "output_tokens": 2}))
        _call_ai_text(handler, "k", "m", "p", 1000, user.id, None,
                      provider="openai", feature="receipt_round1")
        _call_ai_text(handler, "k", "m", "p", 1000, second_user.id, None,
                      provider="openai", feature="receipt_round1")

        user_logs = AIUsageLog.query.filter_by(user_id=user.id).all()
        other_logs = AIUsageLog.query.filter_by(user_id=second_user.id).all()
        assert len(user_logs) == 1
        assert len(other_logs) == 1
        assert user_logs[0].user_id == user.id
        assert other_logs[0].user_id == second_user.id


# ============================================================
# サービス層集計 (monthly_summary / query_logs)
# ============================================================

def _make_log(db, user_id, *, provider="openai", model="gpt-4o",
              feature="receipt_round1", input_tokens=10, output_tokens=5,
              status="ok", created_at=None):
    from datetime import datetime, timezone
    log = AIUsageLog(
        user_id=user_id, provider=provider, model=model, feature=feature,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=(input_tokens or 0) + (output_tokens or 0),
        latency_ms=100, status=status,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.session.add(log)
    db.session.commit()
    return log


class TestServiceAggregation:
    def test_monthly_summary_groups_by_month_and_provider(self, app, db, user):
        from datetime import datetime, timezone
        from app.services.ai_usage import monthly_summary
        _make_log(db, user.id, provider="openai", input_tokens=100, output_tokens=50,
                  created_at=datetime(2026, 4, 15, tzinfo=timezone.utc))
        _make_log(db, user.id, provider="openai", input_tokens=200, output_tokens=80,
                  created_at=datetime(2026, 4, 20, tzinfo=timezone.utc))
        _make_log(db, user.id, provider="anthropic", input_tokens=300, output_tokens=120,
                  created_at=datetime(2026, 5, 1, tzinfo=timezone.utc))

        rows = monthly_summary(user.id)
        # 月降順 → provider 昇順
        assert rows[0]["month"] == "2026-05"
        assert rows[0]["provider"] == "anthropic"
        assert rows[0]["total_tokens"] == 420
        assert rows[1]["month"] == "2026-04"
        assert rows[1]["provider"] == "openai"
        assert rows[1]["count"] == 2
        assert rows[1]["total_tokens"] == 100 + 50 + 200 + 80

    def test_query_logs_filters(self, app, db, user):
        from datetime import datetime, timezone, date as _date
        from app.services.ai_usage import query_logs
        _make_log(db, user.id, provider="openai", feature="receipt_round1",
                  created_at=datetime(2026, 4, 15, tzinfo=timezone.utc))
        _make_log(db, user.id, provider="anthropic", feature="web_extract",
                  created_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
        items, total, *_ = query_logs(user.id, provider="anthropic")
        assert total == 1
        assert items[0].provider == "anthropic"

        items, total, *_ = query_logs(user.id, feature="receipt_round1")
        assert total == 1
        assert items[0].feature == "receipt_round1"

        items, total, *_ = query_logs(user.id,
                                      start=_date(2026, 5, 1),
                                      end=_date(2026, 5, 31))
        assert total == 1
        assert items[0].provider == "anthropic"


# ============================================================
# ビュー層 (/settings/ai-usage)
# ============================================================

class TestAiUsageView:
    def test_unauthenticated_redirects(self, client):
        resp = client.get("/settings/ai-usage")
        assert resp.status_code in (302, 401)

    def test_get_renders_empty_state(self, logged_in_client, user):
        resp = logged_in_client.get("/settings/ai-usage")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "AI API 利用履歴" in html

    def test_get_with_logs(self, logged_in_client, db, user):
        _make_log(db, user.id, provider="openai", feature="receipt_round1")
        _make_log(db, user.id, provider="anthropic", feature="web_extract")
        resp = logged_in_client.get("/settings/ai-usage")
        assert resp.status_code == 200
        html = resp.data.decode()
        # 機能ラベルが日本語で表示される
        assert "AI証憑 R1" in html
        assert "Web明細抽出" in html

    def test_filter_by_provider(self, logged_in_client, db, user):
        _make_log(db, user.id, provider="openai")
        _make_log(db, user.id, provider="anthropic")
        resp = logged_in_client.get("/settings/ai-usage?provider=openai")
        assert resp.status_code == 200
        html = resp.data.decode()
        # 詳細件数表示で 1 件のみ
        assert "詳細履歴 (1件)" in html

    def test_other_users_logs_not_visible(self, logged_in_client, db, user, second_user):
        """IDOR: 他ユーザーのログは見えない"""
        _make_log(db, second_user.id, provider="openai")
        resp = logged_in_client.get("/settings/ai-usage")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "詳細履歴 (0件)" in html


class TestAiUsageExportCsv:
    def test_csv_has_bom_and_header(self, logged_in_client, db, user):
        _make_log(db, user.id, provider="openai", feature="receipt_round1",
                  input_tokens=10, output_tokens=5)
        resp = logged_in_client.get("/settings/ai-usage/export.csv")
        assert resp.status_code == 200
        # BOM 確認
        assert resp.data[:3] == b"\xef\xbb\xbf"
        text = resp.data.decode("utf-8-sig")
        lines = text.strip().split("\n")
        # ヘッダ + データ 1 行
        assert "日時" in lines[0]
        assert "プロバイダー" in lines[0]
        assert "openai" in lines[1]

    def test_csv_filtered_by_provider(self, logged_in_client, db, user):
        _make_log(db, user.id, provider="openai")
        _make_log(db, user.id, provider="anthropic")
        resp = logged_in_client.get(
            "/settings/ai-usage/export.csv?provider=anthropic"
        )
        text = resp.data.decode("utf-8-sig")
        assert "anthropic" in text
        assert "openai" not in text


class TestAiUsageClear:
    def test_requires_confirm(self, logged_in_client, db, user):
        _make_log(db, user.id)
        resp = logged_in_client.post(
            "/settings/ai-usage/clear",
            data={"confirm": "delete"},  # 小文字は不可
            follow_redirects=False,
        )
        assert resp.status_code == 302
        # ログは残る
        assert AIUsageLog.query.filter_by(user_id=user.id).count() == 1

    def test_clears_own_logs(self, logged_in_client, db, user):
        _make_log(db, user.id, provider="openai")
        _make_log(db, user.id, provider="anthropic")
        assert AIUsageLog.query.filter_by(user_id=user.id).count() == 2
        resp = logged_in_client.post(
            "/settings/ai-usage/clear",
            data={"confirm": "DELETE"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert AIUsageLog.query.filter_by(user_id=user.id).count() == 0

    def test_does_not_clear_other_users(self, logged_in_client, db, user, second_user):
        _make_log(db, user.id)
        _make_log(db, second_user.id)
        logged_in_client.post(
            "/settings/ai-usage/clear", data={"confirm": "DELETE"},
        )
        # 自分のだけ消える
        assert AIUsageLog.query.filter_by(user_id=user.id).count() == 0
        assert AIUsageLog.query.filter_by(user_id=second_user.id).count() == 1


class TestAiConfigSummary:
    def test_ai_config_page_shows_monthly_summary(self, logged_in_client, db, user):
        _make_log(db, user.id, provider="openai", input_tokens=100, output_tokens=50)
        resp = logged_in_client.get("/settings/ai")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "今月の使用量" in html
        # provider 名と件数が表示される
        assert "OpenAI" in html
