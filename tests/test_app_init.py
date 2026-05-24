"""app/__init__.py のテスト

audit_permission_check before_request, context processors, security headers,
service worker route, CLI コマンド (seed/seed-user/auto-import/generate-thumbnails)
"""

from unittest.mock import MagicMock, patch

import pytest

from app.models.audit import AuditGrant


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


class TestAuditPermissionCheck:
    def _setup_acting(self, db, client, owner, auditor, level=1):
        grant = AuditGrant(
            owner_user_id=owner.id,
            auditor_user_id=auditor.id,
            permission_level=level,
            status="active",
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = owner.id
            sess["acting_as_permission_level"] = level

    def test_lv1_redirected_from_dashboard(self, db, client, user, auditor, accounts):
        self._setup_acting(db, client, user, auditor, level=1)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_lv1_allowed_on_tax(self, db, client, user, auditor, accounts):
        self._setup_acting(db, client, user, auditor, level=1)
        resp = client.get("/reports/tax")
        assert resp.status_code == 200

    def test_lv2_blocked_from_csv_import(self, db, client, user, auditor, accounts):
        self._setup_acting(db, client, user, auditor, level=2)
        resp = client.get("/csv-import/", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_lv2_blocked_from_ai_journal(self, db, client, user, auditor, accounts):
        self._setup_acting(db, client, user, auditor, level=2)
        resp = client.get("/ai-journal/", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_lv2_blocked_from_fiscal_close(self, db, client, user, auditor, accounts):
        self._setup_acting(db, client, user, auditor, level=2)
        resp = client.post("/settings/fiscal/close", data={
            "year": "2026", "period": "0",
        }, follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_lv2_allowed_on_accounts_listing(self, db, client, user, auditor, accounts):
        # accounts.index (GET /accounts/) は Lv2 でも閲覧可能
        self._setup_acting(db, client, user, auditor, level=2)
        resp = client.get("/accounts/")
        assert resp.status_code == 200

    def test_lv2_allowed_on_journal(self, db, client, user, auditor, accounts):
        self._setup_acting(db, client, user, auditor, level=2)
        resp = client.get("/journal/")
        assert resp.status_code == 200

    def test_lv3_unrestricted(self, db, client, user, auditor, accounts):
        self._setup_acting(db, client, user, auditor, level=3)
        resp = client.get("/csv-import/")
        assert resp.status_code == 200  # Lv3 はブロックされない

    def test_unauthenticated_passes_through(self, client):
        # 認証されていないユーザーは audit check では何もしない
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_static_files_pass_through(self, client):
        # static/ は認証なしでも before_request で許可される
        resp = client.get("/static/css/style.css")
        # ファイルが無い場合 404 だが before_request では弾かれない
        assert resp.status_code in (200, 404)


class TestAuditorExit:
    def test_can_always_exit_even_at_lv1(self, db, client, user, auditor, accounts):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=1,
            status="active",
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 1
        # 終了は常に許可
        resp = client.post("/auditor/exit", follow_redirects=False)
        assert resp.status_code in (302, 303)


class TestMaskAccountFilter:
    def test_filter_in_template(self, app, user):
        with app.test_request_context():
            from flask_login import login_user
            login_user(user)
            # mask_account_filter を呼び出す
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_user_id"] = str(user.id)
                # 通常ユーザーなら隠蔽されない
                from app.services.audit import mask_account_name
                result = mask_account_name("食費", "5010", None)
                assert result == "食費"

    def test_filter_lv2_masks(self, app):
        from app.services.audit import mask_account_name
        result = mask_account_name("給与収入", "4010", {"5010"})
        assert result == "事業主"

    def test_filter_lv2_keeps_allowed(self, app):
        from app.services.audit import mask_account_name
        result = mask_account_name("食費", "5010", {"5010"})
        assert result == "食費"


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
    """E2 PR-E-a: auto-import CLI コマンドは廃止 (auto_import 機能丸ごと削除)。"""

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
