"""WebDAV ファイルソース (services/sources/webdav.py) のテスト"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.sources.webdav import WebDAVProvider


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
