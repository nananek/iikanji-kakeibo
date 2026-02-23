"""Webhook 通知サービス"""

import logging

import httpx

logger = logging.getLogger(__name__)


def send_webhook(
    provider: str,
    url: str,
    title: str,
    message: str,
    details: dict | None = None,
    link_url: str | None = None,
) -> str | None:
    """Webhook 通知を送信する。成功時はメッセージ ID（文字列）を返す。失敗時は None。"""
    sender = _SENDERS.get(provider)
    if not sender:
        logger.warning("Unknown notification provider: %s", provider)
        return None
    return sender(url, title, message, details, link_url)


def update_discord_message(
    webhook_url: str,
    message_id: str,
    title: str,
    message: str,
    details: dict | None = None,
) -> bool:
    """Discord Webhook のメッセージを編集する。"""
    embed: dict = {
        "title": title,
        "description": message,
        "color": 0x9E9E9E,  # grey
    }
    if details:
        embed["fields"] = [
            {"name": k, "value": str(v), "inline": True}
            for k, v in details.items()
        ]

    payload = {"embeds": [embed], "components": []}
    try:
        patch_url = f"{webhook_url}/messages/{message_id}"
        resp = httpx.patch(patch_url, json=payload, timeout=10.0)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Discord message update failed: %s", e)
        return False


def _send_discord(
    url: str,
    title: str,
    message: str,
    details: dict | None = None,
    link_url: str | None = None,
) -> str | None:
    """Discord Webhook (embed 形式)。成功時はメッセージ ID を返す。"""
    embed: dict = {
        "title": title,
        "description": message,
        "color": 0x4CAF50,  # green
    }
    if link_url:
        embed["url"] = link_url
    if details:
        embed["fields"] = [
            {"name": k, "value": str(v), "inline": True}
            for k, v in details.items()
        ]

    payload: dict = {"embeds": [embed]}

    # URL ボタンを追加（link_url がある場合）
    if link_url:
        payload["components"] = [
            {
                "type": 1,  # ActionRow
                "components": [
                    {
                        "type": 2,      # Button
                        "style": 5,     # Link
                        "label": "下書きを確認",
                        "url": link_url,
                    }
                ],
            }
        ]

    # ?wait=true を付けるとレスポンスにメッセージオブジェクトが返る
    post_url = url if "?" in url else f"{url}?wait=true"
    try:
        resp = httpx.post(post_url, json=payload, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return data.get("id")
    except Exception as e:
        logger.error("Discord webhook failed: %s", e)
        return None


_SENDERS = {
    "discord": _send_discord,
}
