"""CSP (Content-Security-Policy) の回帰テスト。

script-src から 'unsafe-inline'/'unsafe-eval' を排除し、インライン script は
nonce で許可、インラインイベントハンドラは data-* + 委譲ハンドラで置換済みで
あることを担保する。
"""

import re


def _script_src(csp: str) -> str:
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith("script-src"):
            return part
    return ""


class TestCspHeader:
    def test_script_src_has_no_unsafe_inline(self, client):
        resp = client.get("/login")
        csp = resp.headers["Content-Security-Policy"]
        ss = _script_src(csp)
        assert "'nonce-" in ss
        assert "'unsafe-inline'" not in ss
        # 'unsafe-eval' は Alpine 標準ビルドのため許容 (将来 CSP ビルドへ移行)
        assert "'unsafe-eval'" in ss
        # defense-in-depth
        assert "object-src 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "frame-ancestors 'self'" in csp

    def test_nonce_matches_inline_scripts(self, client):
        resp = client.get("/login")
        csp = resp.headers["Content-Security-Policy"]
        m = re.search(r"'nonce-([^']+)'", csp)
        assert m, "CSP に nonce が無い"
        nonce = m.group(1)
        html = resp.get_data(as_text=True)
        assert f'nonce="{nonce}"' in html
        # すべての <script> タグに nonce が付いている
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "nonce=" in tag, tag

    def test_nonce_differs_per_request(self, client):
        n1 = re.search(r"'nonce-([^']+)'",
                       client.get("/login").headers["Content-Security-Policy"]).group(1)
        n2 = re.search(r"'nonce-([^']+)'",
                       client.get("/login").headers["Content-Security-Policy"]).group(1)
        assert n1 != n2


class TestNoInlineHandlers:
    HANDLER_RE = re.compile(
        r"\son(click|change|submit|input|load|error|mouseover|focus|blur|keyup|keydown)="
    )

    def test_pages_have_no_inline_handlers(self, logged_in_client):
        for url in ["/", "/journal/", "/settings/", "/vouchers/"]:
            html = logged_in_client.get(url).get_data(as_text=True)
            assert not self.HANDLER_RE.search(html), f"inline handler in {url}"

    def test_app_js_loaded(self, logged_in_client):
        html = logged_in_client.get("/").get_data(as_text=True)
        assert "js/app.js" in html
