"""SSRF 対策 URL バリデーション (services/sources/__init__.py) のテスト"""

from unittest.mock import patch

from app.services.sources import validate_external_url


class TestValidateExternalUrl:
    def test_valid_https(self):
        # 公開ドメイン (DNS 解決可能なら True)
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, '', ('8.8.8.8', 0))]
            ok, err = validate_external_url("https://example.com/path")
            assert ok is True
            assert err is None

    def test_invalid_url_format(self):
        # urlparse が例外を投げる URL は実際には少ないが念のため
        with patch("app.services.sources.urlparse") as mock_parse:
            mock_parse.side_effect = ValueError("bad")
            ok, err = validate_external_url("xxx")
            assert ok is False
            assert "形式" in err

    def test_disallowed_scheme(self):
        ok, err = validate_external_url("ftp://example.com/")
        assert ok is False
        assert "スキーム" in err

    def test_file_scheme(self):
        ok, err = validate_external_url("file:///etc/passwd")
        assert ok is False

    def test_no_hostname(self):
        ok, err = validate_external_url("https:///path")
        assert ok is False
        assert "ホスト名" in err

    def test_with_username(self):
        ok, err = validate_external_url("https://user@example.com/")
        assert ok is False
        assert "ユーザー情報" in err

    def test_with_password(self):
        ok, err = validate_external_url("https://user:pass@example.com/")
        assert ok is False

    def test_loopback_ip(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, '', ('127.0.0.1', 0))]
            ok, err = validate_external_url("https://localhost/")
            assert ok is False
            assert "プライベート" in err

    def test_private_ip_10(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, '', ('10.0.0.1', 0))]
            ok, err = validate_external_url("https://x.example.com/")
            assert ok is False

    def test_private_ip_192_168(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, '', ('192.168.1.1', 0))]
            ok, err = validate_external_url("https://router.local/")
            assert ok is False

    def test_link_local(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, '', ('169.254.169.254', 0))]
            ok, err = validate_external_url("https://metadata/")
            assert ok is False

    def test_reserved_ip(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, '', ('0.0.0.0', 0))]
            ok, err = validate_external_url("https://x/")
            assert ok is False

    def test_dns_resolution_failure(self):
        import socket as _socket
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.side_effect = _socket.gaierror("not resolved")
            ok, err = validate_external_url("https://nonexistent.invalid/")
            assert ok is False
            assert "解決" in err

    def test_ipv6_loopback(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(10, 1, 6, '', ('::1', 0, 0, 0))]
            ok, err = validate_external_url("https://x/")
            assert ok is False

    def test_multiple_resolutions_one_private_blocks(self):
        """1つでもプライベートが含まれれば拒否"""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, '', ('8.8.8.8', 0)),  # 公開
                (2, 1, 6, '', ('192.168.0.1', 0)),  # プライベート
            ]
            ok, err = validate_external_url("https://example.com/")
            assert ok is False
