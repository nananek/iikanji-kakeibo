"""WebDAV ファイルソース (services/sources/webdav.py) のテスト"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.sources.webdav import WebDAVProvider


@pytest.fixture(autouse=True)
def _mock_dns_to_public(monkeypatch):
    """example.com 等のテスト用ホスト名を公開 IP に解決させ、実DNSを叩かない。
    SSRF 検証ロジック自体のテストは getaddrinfo を直接モックして上書きする。"""
    def _fake(host, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr("socket.getaddrinfo", _fake)


class TestWebDAVInit:
    def test_http_allowed(self):
        p = WebDAVProvider("http://example.com/dav", "user", "pass")
        assert p.url == "http://example.com/dav/"

    def test_https_allowed(self):
        p = WebDAVProvider("https://example.com/dav", "user", "pass")
        assert p.url == "https://example.com/dav/"

    def test_disallowed_scheme(self):
        with pytest.raises(ValueError) as exc:
            WebDAVProvider("ftp://example.com/dav", "u", "p")
        assert "許可されていない" in str(exc.value)

    def test_file_scheme_blocked(self):
        with pytest.raises(ValueError):
            WebDAVProvider("file:///etc/passwd", "u", "p")

    def test_loopback_blocked(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        with pytest.raises(ValueError) as exc:
            WebDAVProvider("http://internal.test/dav", "u", "p")
        assert "プライベート" in str(exc.value) or "ローカル" in str(exc.value)

    def test_loopback_ipv6_blocked(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(10, 1, 6, "", ("::1", 0, 0, 0))],
        )
        with pytest.raises(ValueError) as exc:
            WebDAVProvider("http://internal.test/dav", "u", "p")
        assert "プライベート" in str(exc.value) or "ローカル" in str(exc.value)

    def test_link_local_blocked(self, monkeypatch):
        # AWS / GCP メタデータエンドポイント (169.254.169.254) は link-local
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(2, 1, 6, "", ("169.254.169.254", 0))],
        )
        with pytest.raises(ValueError) as exc:
            WebDAVProvider("http://metadata.test/latest/meta-data/", "u", "p")
        assert "プライベート" in str(exc.value) or "ローカル" in str(exc.value)

    def test_unresolvable_host_blocked(self, monkeypatch):
        import socket as _socket

        def _raise(*a, **kw):
            raise _socket.gaierror("no such host")

        monkeypatch.setattr("socket.getaddrinfo", _raise)
        with pytest.raises(ValueError) as exc:
            WebDAVProvider("http://nonexistent.invalid/dav", "u", "p")
        assert "解決" in str(exc.value)


class TestSafeUrlReValidation:
    """test_connection / list_files / download_file が毎回 URL を再検証することを確認"""

    def test_test_connection_rejects_loopback(self, monkeypatch):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        ok, err = p.test_connection()
        assert ok is False
        assert err is not None

    def test_list_files_rejects_loopback(self, monkeypatch):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        # list_files は例外を握り潰して空リスト返却
        assert p.list_files() == []

    def test_download_file_rejects_loopback(self, monkeypatch):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        with pytest.raises(ValueError):
            p.download_file("receipt.jpg")

    def test_default_extensions(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        assert "jpg" in p.extensions
        assert "png" in p.extensions
        assert "pdf" not in p.extensions

    def test_custom_extensions(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p",
                            file_extensions=["pdf", "DOC"])
        assert p.extensions == {"pdf", "doc"}


class TestTestConnection:
    def test_207_success(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 207
            mock_req.return_value = mock_resp
            ok, err = p.test_connection()
            assert ok is True
            assert err is None

    def test_200_success(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_req.return_value = mock_resp
            ok, err = p.test_connection()
            assert ok is True

    def test_401_failure(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_req.return_value = mock_resp
            ok, err = p.test_connection()
            assert ok is False
            assert "401" in err

    def test_request_error(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.request") as mock_req:
            mock_req.side_effect = httpx.RequestError("network")
            ok, err = p.test_connection()
            assert ok is False
            assert err is not None


class TestListFiles:
    _PROPFIND_RESPONSE = b"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/dav/</d:href>
    <d:propstat>
      <d:prop>
        <d:getcontentlength>0</d:getcontentlength>
      </d:prop>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/receipt.jpg</d:href>
    <d:propstat>
      <d:prop>
        <d:getcontentlength>1024</d:getcontentlength>
        <d:getcontenttype>image/jpeg</d:getcontenttype>
        <d:getetag>"abc123"</d:getetag>
      </d:prop>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/notes.txt</d:href>
    <d:propstat>
      <d:prop>
        <d:getcontentlength>50</d:getcontentlength>
        <d:getcontenttype>text/plain</d:getcontenttype>
        <d:getetag>"def456"</d:getetag>
      </d:prop>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/photo.png</d:href>
    <d:propstat>
      <d:prop>
        <d:getcontentlength>2048</d:getcontentlength>
        <d:getcontenttype>image/png</d:getcontenttype>
        <d:getetag>"xyz789"</d:getetag>
      </d:prop>
    </d:propstat>
  </d:response>
</d:multistatus>"""

    def test_filters_by_extension(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.content = self._PROPFIND_RESPONSE
            mock_resp.raise_for_status.return_value = None
            mock_req.return_value = mock_resp
            files = p.list_files()
            paths = {f.path for f in files}
            # jpg / png のみ、txt は除外
            assert "/dav/receipt.jpg" in paths
            assert "/dav/photo.png" in paths
            assert "/dav/notes.txt" not in paths

    def test_extracts_metadata(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.content = self._PROPFIND_RESPONSE
            mock_resp.raise_for_status.return_value = None
            mock_req.return_value = mock_resp
            files = p.list_files()
            jpg = next(f for f in files if f.path.endswith(".jpg"))
            assert jpg.size == 1024
            assert jpg.mime_type == "image/jpeg"
            assert jpg.etag == "abc123"

    def test_request_error_returns_empty(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.request") as mock_req:
            mock_req.side_effect = httpx.RequestError("network")
            files = p.list_files()
            assert files == []

    def test_url_decodes_href(self):
        body = b"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/dav/%E5%9B%B3%E5%83%8F.jpg</d:href>
    <d:propstat>
      <d:prop>
        <d:getcontentlength>100</d:getcontentlength>
        <d:getcontenttype>image/jpeg</d:getcontenttype>
        <d:getetag>"e1"</d:getetag>
      </d:prop>
    </d:propstat>
  </d:response>
</d:multistatus>"""
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.content = body
            mock_resp.raise_for_status.return_value = None
            mock_req.return_value = mock_resp
            files = p.list_files()
            assert len(files) == 1
            assert "図像" in files[0].path


class TestDownloadFile:
    def test_basic_download(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.content = b"image-bytes"
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp
            data = p.download_file("/dav/receipt.jpg")
            assert data == b"image-bytes"

    def test_relative_path(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.content = b"x"
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp
            p.download_file("receipt.jpg")
            url_arg = mock_get.call_args.args[0]
            assert "receipt.jpg" in url_arg

    def test_full_url(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.content = b"x"
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp
            p.download_file("https://example.com/dav/x.jpg")
            url_arg = mock_get.call_args.args[0]
            assert url_arg == "https://example.com/dav/x.jpg"

    def test_path_traversal_blocked(self):
        p = WebDAVProvider("https://example.com/dav", "u", "p")
        with pytest.raises(ValueError) as exc:
            p.download_file("../../etc/passwd")
        assert "不正" in str(exc.value)
