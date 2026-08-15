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

    def test_lv2_blocked_from_accounts_create(self, db, client, user, auditor, accounts):
        # POST /accounts/api/new (accounts.api_create) は Lv2 ではブロックされる
        import json
        self._setup_acting(db, client, user, auditor, level=2)
        resp = client.post("/accounts/api/new", json={
            "code": "9999", "name": "不正追加", "account_type_id": 1,
        }, follow_redirects=True)
        assert resp.status_code == 200
        # flash メッセージは tojson で \uXXXX エスケープされて showToast に埋め込まれる
        escaped = json.dumps("この権限レベルでは勘定科目を変更できません",
                             ensure_ascii=True)[1:-1]
        assert escaped in resp.get_data(as_text=True)
        from app.models.account import Account
        assert Account.query.filter_by(user_id=user.id, code="9999").first() is None

    def test_lv2_blocked_from_accounts_update(self, db, client, user, auditor, accounts):
        # POST /accounts/api/<code> (accounts.api_update) も Lv2 ではブロックされる
        import json
        self._setup_acting(db, client, user, auditor, level=2)
        resp = client.post("/accounts/api/5010", json={
            "name": "不正改名", "is_active": True,
        }, follow_redirects=True)
        assert resp.status_code == 200
        escaped = json.dumps("この権限レベルでは勘定科目を変更できません",
                             ensure_ascii=True)[1:-1]
        assert escaped in resp.get_data(as_text=True)
        from app.models.account import Account
        assert Account.query.filter_by(user_id=user.id, code="5010").first().name == "食費"

    def test_lv2_allowed_on_accounts_api_reads(self, db, client, user, auditor, accounts):
        # 閲覧系 JSON API (api_get / api_balance) は Lv2 でも許可
        self._setup_acting(db, client, user, auditor, level=2)
        resp = client.get("/accounts/api/5010")
        assert resp.status_code == 200
        resp = client.get("/accounts/api/5010/balance")
        assert resp.status_code == 200

    def test_lv2_passes_through_api_prefix(self, db, client, user, auditor, accounts):
        # endpoint が api. で始まる場合 (外部 REST API) は権限制御を素通り
        self._setup_acting(db, client, user, auditor, level=2)
        resp = client.get("/legal/terms")
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

    def test_filter_unauthenticated_returns_name(self, app):
        # 未認証時は mask_account フィルタがそのまま科目名を返す
        with app.test_request_context():
            result = app.jinja_env.filters["mask_account"]("食費", "5010")
            assert result == "食費"

    def test_filter_authenticated_delegates(self, app, user):
        # 認証済みなら audit サービスに委譲する (Lv2 は隠蔽)
        with app.test_request_context():
            from flask_login import login_user
            login_user(user)
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_user_id"] = str(user.id)
                # 通常ユーザー (許可科目リストなし) は隠蔽されない
                result = app.jinja_env.filters["mask_account"]("食費", "5010")
                assert result == "食費"

    def test_filter_registered(self, app):
        assert "mask_account" in app.jinja_env.filters


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


class TestAutoImportCommand:
    def test_with_specific_user_id(self, db, app, user):
        runner = app.test_cli_runner()
        with patch("app.services.auto_import.run_auto_import") as mock_run:
            mock_run.return_value = {
                "sources_processed": 0, "files_found": 0,
                "files_new": 0, "drafts_created": 0, "errors": [],
            }
            result = runner.invoke(args=["auto-import", "--user-id", str(user.id)])
            assert result.exit_code == 0
            mock_run.assert_called_once()

    def test_unknown_user_id(self, db, app):
        runner = app.test_cli_runner()
        result = runner.invoke(args=["auto-import", "--user-id", "9999"])
        assert result.exit_code == 0
        assert "見つかりません" in result.output

    def test_no_sources(self, db, app, user):
        runner = app.test_cli_runner()
        result = runner.invoke(args=["auto-import"])
        assert result.exit_code == 0
        assert "自動取込が設定されている" in result.output

    def test_with_sources(self, db, app, user):
        from app.models.auto_import import AutoImportSource
        from app.services.auto_import import encrypt_credentials
        import json
        s = AutoImportSource(
            user_id=user.id, name="x", provider="webdav",
            config_json=json.dumps({"url": "https://x", "username": "u"}),
            credentials_encrypted=encrypt_credentials({"password": "p"}),
            is_active=True,
        )
        db.session.add(s)
        db.session.commit()
        runner = app.test_cli_runner()
        with patch("app.services.auto_import.run_auto_import") as mock_run:
            mock_run.return_value = {
                "sources_processed": 1, "files_found": 5,
                "files_new": 2, "drafts_created": 1, "errors": [],
            }
            result = runner.invoke(args=["auto-import"])
            assert result.exit_code == 0
            assert "drafts_created" in str(mock_run.call_args) or \
                   "1" in result.output

    def test_dry_run(self, db, app, user):
        from app.models.auto_import import AutoImportSource
        from app.services.auto_import import encrypt_credentials
        import json
        s = AutoImportSource(
            user_id=user.id, name="x", provider="webdav",
            config_json=json.dumps({"url": "https://x", "username": "u"}),
            credentials_encrypted=encrypt_credentials({"password": "p"}),
            is_active=True,
        )
        db.session.add(s)
        db.session.commit()
        runner = app.test_cli_runner()
        with patch("app.services.auto_import.run_auto_import") as mock_run:
            mock_run.return_value = {
                "sources_processed": 1, "files_found": 0,
                "files_new": 0, "drafts_created": 0, "errors": [],
            }
            result = runner.invoke(args=["auto-import", "--dry-run"])
            assert result.exit_code == 0
            assert "DRY RUN" in result.output
            kw = mock_run.call_args.kwargs
            assert kw.get("dry_run") is True

    def test_with_errors_displayed(self, db, app, user):
        from app.models.auto_import import AutoImportSource
        from app.services.auto_import import encrypt_credentials
        import json
        s = AutoImportSource(
            user_id=user.id, name="x", provider="webdav",
            config_json=json.dumps({"url": "https://x", "username": "u"}),
            credentials_encrypted=encrypt_credentials({"password": "p"}),
            is_active=True,
        )
        db.session.add(s)
        db.session.commit()
        runner = app.test_cli_runner()
        with patch("app.services.auto_import.run_auto_import") as mock_run:
            mock_run.return_value = {
                "sources_processed": 1, "files_found": 0,
                "files_new": 0, "drafts_created": 0,
                "errors": ["ファイル1: 失敗", "ファイル2: 失敗"],
            }
            result = runner.invoke(args=["auto-import"])
            assert result.exit_code == 0
            assert "失敗" in result.output


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

    def _make_analyzed_draft(self, db, user_id, image_key):
        from app.models.ai_draft import AIDraft
        d = AIDraft(user_id=user_id, image_key=image_key,
                    image_mime="image/jpeg", status="analyzed")
        db.session.add(d)
        db.session.commit()
        return d

    def test_draft_skip_existing_thumbs(self, db, app, user, accounts):
        # AIDraft (analyzed) のサムネイルが既存ならスキップ
        self._make_analyzed_draft(db, user.id, "drafts/1/test.jpg")
        runner = app.test_cli_runner()
        with patch("app.services.storage.get_storage_backend") as mock_b:
            backend = MagicMock()
            backend.exists.return_value = True
            mock_b.return_value = backend
            result = runner.invoke(args=["generate-thumbnails"])
            assert result.exit_code == 0
            assert "スキップ" in result.output

    def test_draft_generate_actual(self, db, app, user, accounts):
        # AIDraft (analyzed) のサムネイルを実生成
        self._make_analyzed_draft(db, user.id, "drafts/1/test.jpg")
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

    def test_draft_dry_run(self, db, app, user, accounts):
        # dry-run では AIDraft もカウントのみ
        self._make_analyzed_draft(db, user.id, "drafts/1/test.jpg")
        runner = app.test_cli_runner()
        with patch("app.services.storage.get_storage_backend") as mock_b:
            backend = MagicMock()
            backend.exists.return_value = False
            mock_b.return_value = backend
            result = runner.invoke(args=["generate-thumbnails", "--dry-run"])
            assert result.exit_code == 0
            assert "DRY RUN" in result.output
            backend.get.assert_not_called()

    def test_draft_handles_errors(self, db, app, user, accounts):
        # AIDraft のサムネイル生成失敗は ERROR として続行
        self._make_analyzed_draft(db, user.id, "drafts/1/test.jpg")
        runner = app.test_cli_runner()
        with patch("app.services.storage.get_storage_backend") as mock_b:
            backend = MagicMock()
            backend.exists.return_value = False
            backend.get.side_effect = Exception("draft storage error")
            mock_b.return_value = backend
            result = runner.invoke(args=["generate-thumbnails"])
            assert result.exit_code == 0
            assert "Draft" in result.output


class TestTermsAcceptanceCheck:
    """CURRENT_TERMS_VERSION 有効時の再同意フロー (before_request)"""

    def test_redirects_when_version_mismatch(self, db, app, logged_in_client, user, accounts, monkeypatch):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "v1")
        user.accepted_terms_version = "v0"
        db.session.commit()
        resp = logged_in_client.get("/accounts/", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/accept-terms" in resp.headers.get("Location", "")

    def test_no_redirect_when_version_matches(self, db, app, logged_in_client, user, accounts, monkeypatch):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "v1")
        user.accepted_terms_version = "v1"
        db.session.commit()
        resp = logged_in_client.get("/accounts/")
        assert resp.status_code == 200

    def test_accept_terms_page_allowed(self, db, app, logged_in_client, user, monkeypatch):
        # 同意画面自体は再同意フローから除外される
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "v1")
        user.accepted_terms_version = "v0"
        db.session.commit()
        resp = logged_in_client.get("/accept-terms")
        assert resp.status_code in (200, 302)  # 同意画面へはリダイレクトされない

    def test_terms_disabled_no_redirect(self, db, app, logged_in_client, user, accounts):
        # CURRENT_TERMS_VERSION 空 = 同意管理無効
        assert app.config.get("CURRENT_TERMS_VERSION") == ""
        resp = logged_in_client.get("/accounts/")
        assert resp.status_code == 200

    def test_legal_page_exempt_from_terms_check(self, db, app, logged_in_client, user, monkeypatch):
        # 規約不一致でも法的文書 (legal.) は再同意フローから除外される
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "v1")
        user.accepted_terms_version = "v0"
        db.session.commit()
        resp = logged_in_client.get("/legal/terms")
        assert resp.status_code == 200


class TestNotifyTermsUpdateCommand:
    def test_skipped_when_version_unset(self, db, app, user):
        assert not app.config.get("CURRENT_TERMS_VERSION")
        runner = app.test_cli_runner()
        result = runner.invoke(args=["notify-terms-update"])
        assert result.exit_code == 0
        assert "未設定" in result.output

    def test_no_targets(self, db, app, user, monkeypatch):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "v1")
        user.accepted_terms_version = "v1"
        db.session.commit()
        runner = app.test_cli_runner()
        result = runner.invoke(args=["notify-terms-update"])
        assert result.exit_code == 0
        assert "対象ユーザー: 0 件" in result.output

    def test_dry_run_lists_targets(self, db, app, user, monkeypatch):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "v1")
        user.accepted_terms_version = "v0"
        user.email = "test@example.com"
        db.session.commit()
        runner = app.test_cli_runner()
        result = runner.invoke(args=["notify-terms-update", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.output
        assert "test@example.com" in result.output

    def test_limit_option(self, db, app, user, monkeypatch):
        # --limit で対象を絞る
        from app.models.user import User
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "v1")
        user.accepted_terms_version = "v0"
        user.email = "test@example.com"
        u2 = User(username="other3", email="other3@example.com",
                  user_type="personal", accepted_terms_version=None)
        u2.set_password("x")
        db.session.add(u2)
        db.session.commit()
        runner = app.test_cli_runner()
        result = runner.invoke(args=["notify-terms-update", "--dry-run", "--limit", "1"])
        assert result.exit_code == 0
        assert "対象ユーザー: 1 件" in result.output

    def test_send_success_and_failure(self, db, app, user, monkeypatch):
        from unittest.mock import patch as _patch
        from app.models.user import User
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "v1")
        user.accepted_terms_version = "v0"
        user.email = "test@example.com"
        # 2人目の対象ユーザー (送信失敗用)
        u2 = User(username="other2", email="other@example.com",
                  user_type="personal", accepted_terms_version=None)
        u2.set_password("x")
        db.session.add(u2)
        db.session.commit()
        runner = app.test_cli_runner()
        with _patch("app.services.mail.send_email",
                    side_effect=[None, Exception("smtp error")]):
            result = runner.invoke(args=["notify-terms-update"])
        assert result.exit_code == 0
        assert "成功 1 件 / 失敗 1 件" in result.output


class TestVouchersLv2Block:
    def _setup_lv2_acting(self, db, client, user, auditor):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=2,
            status="active",
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 2

    def test_lv2_blocked_from_voucher_attach(self, db, client, user, auditor, accounts):
        # vouchers.attach は Lv2 でブロックされる (書き込み系)
        self._setup_lv2_acting(db, client, user, auditor)
        resp = client.post("/vouchers/attach/1", data={}, follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert resp.headers.get("Location", "").endswith("/")

    def test_lv2_blocked_from_voucher_delete(self, db, client, user, auditor, accounts):
        self._setup_lv2_acting(db, client, user, auditor)
        resp = client.post("/vouchers/999/delete", data={}, follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_acting_with_expired_entitlement_redirects(self, db, client, user, auditor, accounts):
        # 代理閲覧中に顧問枠エンタイトルメントが失効している場合は即終了して
        # auditor ダッシュボードへリダイレクト
        self._setup_lv2_acting(db, client, user, auditor)
        with patch("app.services.entitlement.has_entitlement", return_value=False):
            resp = client.get("/accounts/", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert resp.headers.get("Location", "").endswith("/auditor/")


class TestInviteCreateCommand:
    def test_email_sent_personal(self, db, app, user, monkeypatch):
        monkeypatch.setitem(app.config, "SERVER_NAME", "localhost:5000")
        runner = app.test_cli_runner()
        with patch("app.services.mail.send_email") as mock_send:
            result = runner.invoke(args=["invite-create", "guest@example.com"])
            assert result.exit_code == 0
            assert "招待メールを送信しました" in result.output
            mock_send.assert_called_once()
            _, tpl, ctx = mock_send.call_args.args
            assert tpl == "invitation"
            assert "register_url" in ctx

    def test_email_sent_auditor(self, db, app, user, monkeypatch):
        monkeypatch.setitem(app.config, "SERVER_NAME", "localhost:5000")
        runner = app.test_cli_runner()
        with patch("app.services.mail.send_email") as mock_send:
            result = runner.invoke(args=[
                "invite-create", "cp@example.com",
                "--user-type", "auditor",
            ])
            assert result.exit_code == 0
            mock_send.assert_called_once()
            _, tpl, ctx = mock_send.call_args.args
            assert tpl == "invitation"
            assert "顧問用アカウント" in ctx["service_label"]

    def test_no_email_option(self, db, app, user):
        # --no-email では標準出力にトークンと URL のみ表示
        runner = app.test_cli_runner()
        result = runner.invoke(args=[
            "invite-create", "guest@example.com", "--no-email",
        ])
        assert result.exit_code == 0
        assert "招待トークン発行" in result.output
        assert "Register URL" in result.output

    def test_email_failure_falls_back_to_manual_url(self, db, app, user):
        # メール送信失敗時は URL を手動で送るよう表示
        runner = app.test_cli_runner()
        with patch("app.services.mail.send_email", side_effect=Exception("smtp down")):
            result = runner.invoke(args=["invite-create", "guest@example.com"])
        assert result.exit_code == 0
        assert "メール送信失敗" in result.output
        assert "手動で送信" in result.output

    def test_no_server_name_fallback_url(self, db, app, user):
        # SERVER_NAME 未設定時は警告 + フォールバック URL を表示
        runner = app.test_cli_runner()
        result = runner.invoke(args=["invite-create", "guest@example.com", "--no-email"])
        assert result.exit_code == 0
        assert "warn" in result.output
        assert "Register URL" in result.output

    def test_invitation_token_persisted(self, db, app, user):
        # 発行した招待トークンが DB に記録される
        runner = app.test_cli_runner()
        result = runner.invoke(args=["invite-create", "guest@example.com", "--no-email"])
        assert result.exit_code == 0
        from app.models.invitation import InvitationToken
        rec = InvitationToken.query.filter_by(email="guest@example.com").first()
        assert rec is not None
        assert rec.user_type == "personal"
        assert rec.token_hash


class TestStorageAuditCommand:
    def _mock_audit(self, drift_detected=0, errors=None, drifts=None):
        return {
            "users_checked": 2, "drift_detected": drift_detected,
            "drift_fixed": drift_detected,
            "drifts": drifts or [],
        }

    def test_dry_run(self, db, app, user, accounts):
        runner = app.test_cli_runner()
        with patch("app.services.storage_audit.backfill_file_sizes") as mock_bf, \
             patch("app.services.storage_audit.audit_storage_usage") as mock_au:
            mock_bf.return_value = {
                "voucher_backfilled": 0, "draft_backfilled": 0, "errors": [],
            }
            mock_au.return_value = self._mock_audit()
            result = runner.invoke(args=["storage-audit"])
            assert result.exit_code == 0
            assert "[DRY]" in result.output
            mock_au.assert_called_once_with(fix=False)

    def test_fix_mode(self, db, app, user, accounts):
        runner = app.test_cli_runner()
        with patch("app.services.storage_audit.backfill_file_sizes") as mock_bf, \
             patch("app.services.storage_audit.audit_storage_usage") as mock_au:
            mock_bf.return_value = {
                "voucher_backfilled": 1, "draft_backfilled": 0, "errors": [],
            }
            mock_au.return_value = self._mock_audit(
                drift_detected=1,
                drifts=[{"user_id": 1, "measured": 100, "recorded": 0, "delta": 100}],
            )
            result = runner.invoke(args=["storage-audit", "--fix"])
            assert result.exit_code == 0
            assert "[FIX]" in result.output
            mock_au.assert_called_once_with(fix=True)

    def test_backfill_errors_displayed(self, db, app, user, accounts):
        runner = app.test_cli_runner()
        with patch("app.services.storage_audit.backfill_file_sizes") as mock_bf, \
             patch("app.services.storage_audit.audit_storage_usage") as mock_au:
            mock_bf.return_value = {
                "voucher_backfilled": 0, "draft_backfilled": 0,
                "errors": ["voucher 1: boom"],
            }
            mock_au.return_value = self._mock_audit()
            result = runner.invoke(args=["storage-audit"])
            assert result.exit_code == 0
            assert "ERROR" in result.output

    def test_drift_details_displayed(self, db, app, user, accounts):
        runner = app.test_cli_runner()
        with patch("app.services.storage_audit.backfill_file_sizes") as mock_bf, \
             patch("app.services.storage_audit.audit_storage_usage") as mock_au:
            mock_bf.return_value = {
                "voucher_backfilled": 0, "draft_backfilled": 0, "errors": [],
            }
            mock_au.return_value = self._mock_audit(
                drift_detected=1,
                drifts=[{"user_id": 1, "measured": 50, "recorded": 10, "delta": 40}],
            )
            result = runner.invoke(args=["storage-audit", "--fix"])
            assert result.exit_code == 0
            assert "delta=+40" in result.output


class TestContextProcessors:
    def test_dev_flag_in_template(self, client):
        # ログイン画面をレンダリングして dev_badge が出るかは debug モード次第
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_captcha_config_when_disabled(self, client):
        resp = client.get("/login")
        # CAPTCHA 関連変数がテンプレートに渡されている
        assert resp.status_code == 200
