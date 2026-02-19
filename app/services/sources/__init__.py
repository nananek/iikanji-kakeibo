"""外部ファイルソースの抽象化"""

import ipaddress
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse


@dataclass
class SourceFile:
    """ソースから取得したファイルのメタデータ"""

    path: str
    etag: str | None
    size: int
    mime_type: str | None


class FileSourceProvider(Protocol):
    """ファイルソースプロバイダーのプロトコル"""

    def list_files(self) -> list[SourceFile]:
        """ファイル一覧を取得"""
        ...

    def download_file(self, path: str) -> bytes:
        """ファイルをダウンロード"""
        ...

    def test_connection(self) -> tuple[bool, str | None]:
        """接続テスト。(成功, エラーメッセージ) を返す"""
        ...


def validate_external_url(url: str) -> tuple[bool, str | None]:
    """SSRF 対策: 外部 URL の安全性を検証する。

    プライベート IP、ループバック、リンクローカルへのアクセスを拒否する。
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
