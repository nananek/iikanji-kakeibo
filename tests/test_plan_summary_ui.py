"""設定画面の「現在のプラン」セクション表示テスト (Phase 2 #67)。

- セルフホストモード (BILLING_BACKEND=unlimited, default) で
  「セルフホストモード — 全機能解放」と表示される
- 認証必須 (未ログインは 302)
- 個人ユーザー / 監査ユーザーの両方で表示される
"""

from app.services.entitlement import UnlimitedBillingClient


class TestPlanSummarySection:
    def test_unlimited_section_visible_for_personal(self, logged_in_client):
        resp = logged_in_client.get("/settings/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "現在のプラン" in body
        assert "セルフホストモード" in body
        assert "全機能解放" in body

    def test_unlimited_section_visible_for_auditor(self, client, auditor):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
        resp = client.get("/settings/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "セルフホストモード" in body

    def test_anonymous_redirects(self, client):
        resp = client.get("/settings/")
        assert resp.status_code in (302, 303)

    def test_http_backend_not_implemented_falls_back(
        self, logged_in_client, app, monkeypatch
    ):
        """Phase 3 未実装の `http` バックエンド設定で 500 にならず、
        セクションが非表示になる (`plan_summary = None`)。
        """
        monkeypatch.setitem(app.config, "BILLING_BACKEND", "http")
        resp = logged_in_client.get("/settings/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # セクション全体が描画されない
        assert "現在のプラン" not in body
        assert "セルフホストモード" not in body

    def test_summary_uses_billing_client(self, logged_in_client, monkeypatch):
        """`get_entitlement_summary` のソースが BillingClient であることを確認。

        差し替えたクライアントの戻り値がテンプレートに反映される。
        """
        class CustomSummaryClient(UnlimitedBillingClient):
            def get_entitlement_summary(self, user):
                return {
                    "mode": "http",
                    "active_features": ["paid_llm", "voucher_storage"],
                }

        from app.services import entitlement as ent
        monkeypatch.setattr(ent, "get_billing_client", lambda: CustomSummaryClient())

        resp = logged_in_client.get("/settings/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # セルフホストではない表示に切り替わる
        assert "セルフホストモード" not in body
        assert "プラン情報" in body
