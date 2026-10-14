"""外部 URL の安全性検証 (SSRF 対策)"""

import ipaddress
import socket
from urllib.parse import urlparse


def validate_external_url(url: str) -> tuple[bool, str | None]:
    """SSRF 対策: 外部 URL の安全性を検証する。

    プライベート IP、ループバック、リンクローカルへのアクセスを拒否する。
    外部 AI の base_url、Webhook URL の検証に使う。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return (False, "URL の形式が不正です")

    if parsed.scheme not in ("http", "https"):
        return (False, f"許可されていないスキームです: {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        return (False, "ホスト名が不正です")

    # ユーザー情報（user:pass@host）を拒否
    if parsed.username or parsed.password:
        return (False, "URL にユーザー情報を含めないでください")

    # DNS 解決してプライベート IP をチェック
    try:
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return (False, "プライベート/ローカルアドレスへのアクセスは禁止されています")
    except socket.gaierror:
        return (False, f"ホスト名を解決できません: {hostname}")

    return (True, None)
