"""法的文書ページ (利用規約 / プライバシーポリシー / 特商法表記) の
テスト (Phase 1 #66)。

- 3 ページがいずれも 200 を返す (認証不要)
- 不正な slug は 404
- 運営者情報 (OPERATOR_*) が context に注入されてテンプレートに反映
- 環境変数未設定時は "(未設定)" プレースホルダで表示される
- フッター (base.html) に 3 つのリンクが含まれる
- 各テンプレートに損害賠償・API キー漏えい対応の重要条項が含まれる (TOS)
"""

import pytest


class TestLegalPagesPublic:
    """法的文書は未認証でも閲覧できる"""

    @pytest.mark.parametrize("slug, title", [
        ("terms", "利用規約"),
        ("privacy", "プライバシーポリシー"),
        ("tokushoho", "特定商取引法に基づく表記"),
    ])
    def test_get_legal_page(self, client, slug, title):
        resp = client.get(f"/legal/{slug}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert title in body

    def test_unknown_slug_returns_404(self, client):
        resp = client.get("/legal/unknown")
        assert resp.status_code == 404


class TestOperatorInjection:
    """OPERATOR_* config の値がテンプレートに注入される"""

    def test_operator_values_rendered(self, client, app, monkeypatch):
        monkeypatch.setitem(app.config, "OPERATOR_NAME", "山田太郎")
        monkeypatch.setitem(app.config, "OPERATOR_ADDRESS", "東京都千代田区1-1-1")
        monkeypatch.setitem(app.config, "OPERATOR_EMAIL", "ops@example.com")
        monkeypatch.setitem(app.config, "OPERATOR_PHONE", "03-1234-5678")
        monkeypatch.setitem(app.config, "OPERATOR_BUSINESS_FORM", "個人事業主")

        resp = client.get("/legal/tokushoho")
        body = resp.get_data(as_text=True)
        assert "山田太郎" in body
        assert "東京都千代田区1-1-1" in body
        assert "ops@example.com" in body
        assert "03-1234-5678" in body
        assert "個人事業主" in body

    def test_unset_operator_shows_placeholder(self, client, app, monkeypatch):
        monkeypatch.setitem(app.config, "OPERATOR_NAME", "")
        monkeypatch.setitem(app.config, "OPERATOR_ADDRESS", "")
        monkeypatch.setitem(app.config, "OPERATOR_EMAIL", "")
        resp = client.get("/legal/tokushoho")
        body = resp.get_data(as_text=True)
        # 空文字でも (未設定) が表示されて 500 にはならない
        assert resp.status_code == 200
        assert "(未設定)" in body


class TestFooterLinks:
    """base.html フッターに 3 リンクが含まれる"""

    def test_footer_has_legal_links(self, client):
        resp = client.get("/login")
        body = resp.get_data(as_text=True)
        assert "/legal/terms" in body
        assert "/legal/privacy" in body
        assert "/legal/tokushoho" in body


class TestImportantClausesPresent:
    """TOS に損害賠償制限・API キー漏えい対応の重要条項が含まれる"""

    def test_terms_has_liability_clause(self, client):
        resp = client.get("/legal/terms")
        body = resp.get_data(as_text=True)
        # 損害賠償の制限条項
        assert "損害賠償" in body
        assert "故意または重過失" in body
        # 賠償上限の言及
        assert "上限" in body
        # 間接損害・逸失利益の免責
        assert "間接損害" in body or "逸失利益" in body

    def test_terms_has_api_key_clauses(self, client):
        resp = client.get("/legal/terms")
        body = resp.get_data(as_text=True)
        # API キー暗号化保管の明示
        assert "Fernet" in body
        assert "API キー" in body
        # 漏えい時のローテーション義務
        assert "ローテーション" in body

    def test_privacy_has_security_clause(self, client):
        resp = client.get("/legal/privacy")
        body = resp.get_data(as_text=True)
        # API キー Fernet 暗号化
        assert "Fernet" in body
        assert "API キー" in body
        # セキュリティ対策セクション
        assert "セキュリティ" in body
        # 漏えい時通知
        assert "漏えい" in body or "漏洩" in body

    def test_privacy_lists_data_collected(self, client):
        resp = client.get("/legal/privacy")
        body = resp.get_data(as_text=True)
        # 取得情報のセクションがある
        assert "取得" in body
        assert "保存期間" in body
