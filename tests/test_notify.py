"""Webhook 通知サービス (notify.py) のテスト"""

from unittest.mock import MagicMock, patch

import httpx

from app.services.notify import (
    _send_discord,
    send_webhook,
    update_discord_message,
)


class TestSendWebhook:
    def test_unknown_provider_returns_none(self):
        result = send_webhook(
            provider="slack",  # 未対応
            url="https://example.com/webhook",
            title="t", message="m",
        )
        assert result is None

    def test_discord_dispatch(self):
        # _SENDERS は import 時にバインドされているので httpx.post を直接モック
        with patch("httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"id": "msg-123"}
            mock_resp.raise_for_status.return_value = None
            mock_post.return_value = mock_resp
            result = send_webhook(
                provider="discord",
                url="https://discord.com/api/webhooks/x/y",
                title="t", message="m",
            )
            assert result == "msg-123"


class TestSendDiscord:
    def test_basic_post(self):
        with patch("httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"id": "msg-1"}
            mock_resp.raise_for_status.return_value = None
            mock_post.return_value = mock_resp
            result = _send_discord(
                url="https://discord.com/api/webhooks/x/y",
                title="t", message="m",
            )
            assert result == "msg-1"
            # ?wait=true が付与されている
            call_args = mock_post.call_args
            assert "wait=true" in call_args.args[0]

    def test_with_link_button(self):
        with patch("httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"id": "msg-2"}
            mock_resp.raise_for_status.return_value = None
            mock_post.return_value = mock_resp
            _send_discord(
                url="https://discord.com/api/webhooks/x/y",
                title="t", message="m",
                link_url="https://example.com/drafts",
            )
            payload = mock_post.call_args.kwargs["json"]
            assert "components" in payload
            assert payload["components"][0]["components"][0]["url"] == \
                "https://example.com/drafts"
            # embed の url にもセット
            assert payload["embeds"][0]["url"] == "https://example.com/drafts"

    def test_with_details(self):
        with patch("httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"id": "msg-3"}
            mock_resp.raise_for_status.return_value = None
            mock_post.return_value = mock_resp
            _send_discord(
                url="https://discord.com/api/webhooks/x/y",
                title="t", message="m",
                details={"金額": "1000円", "店舗": "セブン"},
            )
            payload = mock_post.call_args.kwargs["json"]
            fields = payload["embeds"][0]["fields"]
            assert len(fields) == 2

    def test_already_has_query_string(self):
        with patch("httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"id": "msg-4"}
            mock_resp.raise_for_status.return_value = None
            mock_post.return_value = mock_resp
            _send_discord(
                url="https://discord.com/api/webhooks/x/y?wait=true",
                title="t", message="m",
            )
            # ?wait=true は重複しない
            url_arg = mock_post.call_args.args[0]
            assert url_arg.count("?") == 1

    def test_http_error_returns_none(self, caplog):
        with patch("httpx.post") as mock_post:
            mock_post.side_effect = httpx.RequestError("network down")
            result = _send_discord(
                url="https://discord.com/api/webhooks/x/y",
                title="t", message="m",
            )
            assert result is None

    def test_status_error_returns_none(self):
        with patch("httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock()
            )
            mock_post.return_value = mock_resp
            result = _send_discord(
                url="https://discord.com/api/webhooks/x/y",
                title="t", message="m",
            )
            assert result is None


class TestUpdateDiscordMessage:
    def test_basic_patch(self):
        with patch("httpx.patch") as mock_patch:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_patch.return_value = mock_resp
            result = update_discord_message(
                webhook_url="https://discord.com/api/webhooks/x/y",
                message_id="msg-1",
                title="updated",
                message="done",
            )
            assert result is True
            # PATCH /messages/<id>
            call_args = mock_patch.call_args
            assert "messages/msg-1" in call_args.args[0]

    def test_with_details(self):
        with patch("httpx.patch") as mock_patch:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_patch.return_value = mock_resp
            result = update_discord_message(
                webhook_url="https://discord.com/api/webhooks/x/y",
                message_id="msg-1",
                title="t", message="m",
                details={"key": "val"},
            )
            assert result is True
            payload = mock_patch.call_args.kwargs["json"]
            assert "fields" in payload["embeds"][0]

    def test_clears_components(self):
        """編集時は components を空にしてボタンを消す"""
        with patch("httpx.patch") as mock_patch:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_patch.return_value = mock_resp
            update_discord_message(
                webhook_url="https://discord.com/api/webhooks/x/y",
                message_id="msg-1", title="t", message="m",
            )
            payload = mock_patch.call_args.kwargs["json"]
            assert payload["components"] == []

    def test_error_returns_false(self):
        with patch("httpx.patch") as mock_patch:
            mock_patch.side_effect = httpx.RequestError("down")
            result = update_discord_message(
                webhook_url="https://discord.com/api/webhooks/x/y",
                message_id="msg-1", title="t", message="m",
            )
            assert result is False
