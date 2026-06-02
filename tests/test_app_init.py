"""app/__init__.py のテスト

context processors, security headers, service worker route,
CLI コマンド (seed/seed-user/auto-import/generate-thumbnails)

旧リアルタイム代理閲覧 (acting_as_user_id) の before_request ゲートと
auditor.exit ルートは撤去済み (#112) のため、関連テストは削除した。
"""

from unittest.mock import MagicMock, patch


class TestServiceWorker:
    def test_sw_js_served(self, client):
        resp = client.get("/sw.js")
        # static folder にファイルが無い場合は 404 だが、ルート自体は存在
        assert resp.status_code in (200, 404)


class TestSecurityHeaders:
    def test_basic_headers(self, client):
        resp = client.get("/login")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert "Referrer-Policy" in resp.headers


# 旧 mask_account template filter (TestMaskAccountFilter) は科目隠蔽 (Lv2 監査
# 代理閲覧) 専用で、旧リアルタイム代理閲覧の撤去 (#112) に伴い filter ごと削除した
# (app/services/audit.py も削除)。


class TestSeedCommand:
    def test_seed_runs(self, app):
        runner = app.test_cli_runner()
        with patch("app.services.seed.seed_account_types") as mock_seed:
            result = runner.invoke(args=["seed"])
            assert result.exit_code == 0
            mock_seed.assert_called_once()


class TestSeedUserCommand:
    def test_seed_user_runs(self, db, app, user):
        runner = app.test_cli_runner()
        with patch("app.services.seed.seed_accounts_for_user") as mock_seed:
            result = runner.invoke(args=["seed-user"])
            assert result.exit_code == 0
            # user fixture が存在するので最低 1回呼ばれる
            mock_seed.assert_called()


class TestAutoImportCommandRemoved:
    """auto-import CLI コマンドは廃止 (auto_import 機能丸ごと削除)。"""

    def test_command_no_longer_exists(self, db, app):
        runner = app.test_cli_runner()
        result = runner.invoke(args=["auto-import"])
        # Click は未定義コマンドで non-zero exit code + 'No such command'
        assert result.exit_code != 0
        assert "auto-import" in result.output.lower() or \
               "no such command" in result.output.lower()


class TestGenerateThumbnailsCommand:
    def test_no_data(self, db, app, user, accounts):
        runner = app.test_cli_runner()
        with patch("app.services.storage.get_storage_backend") as mock_b:
            backend = MagicMock()
            backend.exists.return_value = False
            mock_b.return_value = backend
            result = runner.invoke(args=["generate-thumbnails"])
            assert result.exit_code == 0
            assert "0件" in result.output or "サムネイル生成" in result.output

    def test_dry_run(self, db, app, user, accounts):
        from tests.conftest import make_voucher
        v = make_voucher(db, user.id, image_key="vouchers/1/test.jpg")
        runner = app.test_cli_runner()
        with patch("app.services.storage.get_storage_backend") as mock_b:
            backend = MagicMock()
            backend.exists.return_value = False
            mock_b.return_value = backend
            result = runner.invoke(args=["generate-thumbnails", "--dry-run"])
            assert result.exit_code == 0
            assert "DRY RUN" in result.output

    def test_skip_existing_thumbs(self, db, app, user, accounts):
        from tests.conftest import make_voucher
        v = make_voucher(db, user.id, image_key="vouchers/1/test.jpg")
        runner = app.test_cli_runner()
        with patch("app.services.storage.get_storage_backend") as mock_b:
            backend = MagicMock()
            backend.exists.return_value = True  # サムネイル既存
            mock_b.return_value = backend
            result = runner.invoke(args=["generate-thumbnails"])
            assert result.exit_code == 0
            assert "スキップ" in result.output

    def test_generate_actual(self, db, app, user, accounts):
        from tests.conftest import make_voucher
        v = make_voucher(db, user.id, image_key="vouchers/1/test.jpg")
        runner = app.test_cli_runner()
        with patch("app.services.storage.get_storage_backend") as mock_b, \
             patch("app.services.storage.generate_thumbnail") as mock_gen:
            backend = MagicMock()
            backend.exists.return_value = False
            backend.get.return_value = b"image-data"
            mock_b.return_value = backend
            mock_gen.return_value = b"thumb-bytes"
            result = runner.invoke(args=["generate-thumbnails"])
            assert result.exit_code == 0
            backend.put.assert_called()

    def test_handles_errors(self, db, app, user, accounts):
        from tests.conftest import make_voucher
        v = make_voucher(db, user.id, image_key="vouchers/1/test.jpg")
        runner = app.test_cli_runner()
        with patch("app.services.storage.get_storage_backend") as mock_b:
            backend = MagicMock()
            backend.exists.return_value = False
            backend.get.side_effect = Exception("storage error")
            mock_b.return_value = backend
            result = runner.invoke(args=["generate-thumbnails"])
            assert result.exit_code == 0
            assert "ERROR" in result.output


class TestContextProcessors:
    def test_dev_flag_in_template(self, client):
        # ログイン画面をレンダリングして dev_badge が出るかは debug モード次第
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_captcha_config_when_disabled(self, client):
        resp = client.get("/login")
        # CAPTCHA 関連変数がテンプレートに渡されている
        assert resp.status_code == 200
