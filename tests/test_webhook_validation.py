"""Webhook 追加のドメイン制限解除 (Discord 互換) のテスト。

ドメインは限定しないが SSRF 対策 (validate_external_url) は維持する。
"""

import json
from unittest.mock import patch

from app.models.auto_import import WebhookConfig


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)


def test_accepts_non_discord_compatible_url(client, user, db):
    """discord.com 以外の Discord 互換 Webhook URL を受理する。"""
    _login(client, user)
    with patch("socket.getaddrinfo") as mock_dns:
        mock_dns.return_value = [(2, 1, 6, "", ("8.8.8.8", 0))]
        resp = client.post(
            "/settings/auto-import/webhooks/add",
            data={
                "name": "自家ホスト通知",
                "provider": "discord",
                "webhook_url": "https://chat.example.com/api/webhooks/123/abc",
                "events": ["import_success"],
            },
            follow_redirects=True,
        )
    assert resp.status_code == 200
    saved = WebhookConfig.query.filter_by(user_id=user.id).first()
    assert saved is not None
    assert saved.webhook_url == "https://chat.example.com/api/webhooks/123/abc"
    assert "import_success" in json.loads(saved.events_json)


def test_still_accepts_discord_com_url(client, user, db):
    _login(client, user)
    with patch("socket.getaddrinfo") as mock_dns:
        mock_dns.return_value = [(2, 1, 6, "", ("1.1.1.1", 0))]
        client.post(
            "/settings/auto-import/webhooks/add",
            data={
                "name": "Discord 通知",
                "provider": "discord",
                "webhook_url": "https://discord.com/api/webhooks/999/xyz",
            },
            follow_redirects=True,
        )
    saved = WebhookConfig.query.filter_by(user_id=user.id).first()
    assert saved is not None
    assert saved.webhook_url.startswith("https://discord.com/api/webhooks/")


def test_rejects_private_ip_ssrf(client, user, db):
    """SSRF: private/loopback へ解決する URL は依然として拒否する。"""
    _login(client, user)
    with patch("socket.getaddrinfo") as mock_dns:
        mock_dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 0))]
        resp = client.post(
            "/settings/auto-import/webhooks/add",
            data={
                "name": "内部",
                "provider": "discord",
                "webhook_url": "https://internal.example.com/api/webhooks/1/a",
            },
            follow_redirects=True,
        )
    assert resp.status_code == 200
    assert WebhookConfig.query.filter_by(user_id=user.id).count() == 0


def test_rejects_non_http_scheme(client, user, db):
    """非 http(s) スキームは拒否する。"""
    _login(client, user)
    resp = client.post(
        "/settings/auto-import/webhooks/add",
        data={
            "name": "ftp",
            "provider": "discord",
            "webhook_url": "ftp://example.com/webhook",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert WebhookConfig.query.filter_by(user_id=user.id).count() == 0
