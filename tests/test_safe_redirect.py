"""is_safe_internal_path のテスト（オープンリダイレクト対策）"""

import pytest

from app.views.helpers import is_safe_internal_path


class TestIsSafeInternalPath:
    @pytest.mark.parametrize("p", [
        "/journal/", "/cashbook/edit/1", "/settings/ai/",
        "/a/b?x=1", "/a/b#frag",
    ])
    def test_internal_paths_allowed(self, p):
        assert is_safe_internal_path(p) is True

    @pytest.mark.parametrize("p", [
        "", None, 123,
        "http://evil.com/x",
        "https://evil.com/x",
        "//evil.com/x",
        "/\\evil.com",
        "javascript:alert(1)",
        "data:text/html,abc",
        "ftp://x/y",
        "evil.com/x",      # 先頭 / なし
        " /journal/",      # 空白始まり
        "//\\evil.com",
    ])
    def test_external_or_invalid_rejected(self, p):
        assert is_safe_internal_path(p) is False
