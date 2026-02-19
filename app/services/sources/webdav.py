"""WebDAV ファイルソースプロバイダー"""

import logging
from urllib.parse import unquote, urljoin
from xml.etree import ElementTree as ET

import httpx

from app.services.sources import SourceFile

logger = logging.getLogger(__name__)

_DAV_NS = "DAV:"

_PROPFIND_BODY = """\
<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <d:getetag/>
  </d:prop>
</d:propfind>"""


class WebDAVProvider:
    """WebDAV (Nextcloud 等) からファイルを取得するプロバイダー"""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        file_extensions: list[str] | None = None,
    ):
        self.url = url.rstrip("/") + "/"
        self.auth = (username, password)
        self.extensions = {
            e.lower() for e in (file_extensions or ["jpg", "jpeg", "png", "webp"])
        }

    def test_connection(self) -> tuple[bool, str | None]:
        try:
            resp = httpx.request(
                "PROPFIND",
                self.url,
                auth=self.auth,
                headers={"Depth": "0"},
                timeout=10.0,
            )
            if resp.status_code in (207, 200):
                return (True, None)
            return (False, f"HTTP {resp.status_code}")
        except httpx.HTTPStatusError as e:
            return (False, f"HTTP {e.response.status_code}")
        except Exception as e:
            return (False, str(e))

    def list_files(self) -> list[SourceFile]:
        try:
            resp = httpx.request(
                "PROPFIND",
                self.url,
                auth=self.auth,
                headers={"Depth": "1", "Content-Type": "application/xml"},
                content=_PROPFIND_BODY.encode(),
                timeout=30.0,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("WebDAV PROPFIND failed: %s", e)
            return []

        ns = {"d": _DAV_NS}
        root = ET.fromstring(resp.content)

        files: list[SourceFile] = []
        for response in root.findall("d:response", ns):
            href_elem = response.find("d:href", ns)
            if href_elem is None or not href_elem.text:
                continue

            href = unquote(href_elem.text)
            if href.endswith("/"):
                continue  # ディレクトリをスキップ

            ext = href.rsplit(".", 1)[-1].lower() if "." in href else ""
            if ext not in self.extensions:
                continue

            propstat = response.find("d:propstat", ns)
            if propstat is None:
                continue
            prop = propstat.find("d:prop", ns)
            if prop is None:
                continue

            size_elem = prop.find("d:getcontentlength", ns)
            type_elem = prop.find("d:getcontenttype", ns)
            etag_elem = prop.find("d:getetag", ns)

            files.append(
                SourceFile(
                    path=href,
                    etag=etag_elem.text.strip('"') if etag_elem is not None and etag_elem.text else None,
                    size=int(size_elem.text) if size_elem is not None and size_elem.text else 0,
                    mime_type=type_elem.text if type_elem is not None else None,
                )
            )

        return files

    def download_file(self, path: str) -> bytes:
        if path.startswith("http"):
            url = path
        elif path.startswith("/"):
            # href が絶対パスの場合、ベースURLのホスト部分と結合
            url = urljoin(self.url, path)
        else:
            url = self.url + path.lstrip("/")

        resp = httpx.get(url, auth=self.auth, timeout=60.0)
        resp.raise_for_status()
        return resp.content
