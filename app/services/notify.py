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
) -> bool:
    """Webhook 通知を送信する。成功時 True を返す。"""
    sender = _SENDERS.get(provider)
    if not sender:
        logger.warning("Unknown notification provider: %s", provider)
        return False
    return sender(url, title, message, details, link_url)


def _send_discord(
    url: str,
    title: str,
    message: str,
    details: dict | None = None,
    link_url: str | None = None,
) -> bool:
    """Discord Webhook (embed 形式)"""
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

    payload = {"embeds": [embed]}
    try:
        resp = httpx.post(url, json=payload, timeout=10.0)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Discord webhook failed: %s", e)
        return False


_SENDERS = {
    "discord": _send_discord,
}
