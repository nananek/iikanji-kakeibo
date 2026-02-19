"""外部ファイルソースの抽象化"""

from dataclasses import dataclass
from typing import Protocol


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
