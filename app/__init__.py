from flask import Flask

from app.config import Config
from app.extensions import db, migrate, login_manager, csrf, limiter


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # Models (import so Alembic can detect them)
    from app import models  # noqa: F401

    # Blueprints
    from app.views import register_blueprints
    register_blueprints(app)

    # JSON API は CSRF 免除
    from app.views.webauthn import bp as webauthn_bp
    csrf.exempt(webauthn_bp)
    from app.views.api import bp as api_bp
    csrf.exempt(api_bp)
    # OAuth Device Flow のクライアント向けエンドポイントは CSRF 免除
    # (ブラウザ向けの authorize エンドポイントは CSRF 保護を維持)
    from app.views.oauth import device_authorization, token as oauth_token_view
    csrf.exempt(device_authorization)
    csrf.exempt(oauth_token_view)

    # リカバリコードでログイン後の強制復旧フロー
    # 「新パスキー登録 + リカバリコード再生成」両達成までは限定ページのみ許可
    @app.before_request
    def pending_recovery_gate():
        from flask import request, redirect, url_for, session
        from flask_login import current_user as cu
        if not cu.is_authenticated:
            return
        if not session.get("pending_recovery_action"):
            return
        # セッション整合性チェック: pending_recovery_user_id が現在のログイン
        # ユーザーと一致しない場合は flag を破棄（別ユーザーで再ログイン等）
        pending_uid = session.get("pending_recovery_user_id")
        if pending_uid is not None and pending_uid != cu.id:
            session.pop("pending_recovery_action", None)
            session.pop("pending_recovery_user_id", None)
            return
        endpoint = request.endpoint or ""
        # 強制復旧の遂行に必要なエンドポイントだけ許可
        allowed_endpoints = {
            "settings.passkeys",
            "settings.delete_passkey",
            "settings.recovery_generate",
            "settings.passkey_only_enable",
            "settings.passkey_only_disable",
            "webauthn.register_options",
            "webauthn.register_verify",
            "auth.logout",
        }
        if endpoint in allowed_endpoints or endpoint.startswith("static"):
            return
        return redirect(url_for("settings.passkeys"))

    # Before-request hook for audit permission control
    @app.before_request
    def audit_permission_check():
        from flask import request, redirect, url_for
        from flask_login import current_user as cu
        if not cu.is_authenticated:
            return
        from app.services.audit import (
            get_permission_level, is_acting_as_auditor, require_permission,
        )
        perm = get_permission_level()
        if perm is None:
            return
        endpoint = request.endpoint or ""
        # Static files, auth, and external API: always allow
        if endpoint.startswith("static") or endpoint.startswith("auth.") or endpoint.startswith("api."):
            return
        # Auditor exit: always allow
        if endpoint == "auditor.exit_acting":
            return
        # Lv1: only tax report
        if perm == 1:
            if endpoint not in ("reports.tax", "reports.medical_csv"):
                return redirect(url_for("reports.tax"))
        # Lv2: block import/AI/account changes/fiscal settings
        elif perm == 2:
            blocked_prefixes = (
                "csv_import.", "web_import.", "ofx_import.", "ai_journal.",
                "settings.fiscal", "settings.fiscal_close", "settings.fiscal_reopen",
            )
            if endpoint in blocked_prefixes or any(endpoint.startswith(p) for p in blocked_prefixes):
                from flask import flash
                flash("この権限レベルではこの機能を使用できません。", "warning")
                return redirect(url_for("dashboard.index"))
            # accounts: block modification (new, edit, toggle, delete)
            if endpoint in ("accounts.new", "accounts.edit", "accounts.toggle", "accounts.delete", "accounts.api_add"):
                from flask import flash
                flash("この権限レベルでは勘定科目を変更できません。", "warning")
                return redirect(url_for("accounts.index"))

    # Context processor for dev flag
    @app.context_processor
    def inject_dev_flag():
        return {"is_dev": app.debug}

    # Context processor for CAPTCHA
    @app.context_processor
    def inject_captcha_config():
        return {
            "captcha_provider": app.config.get("CAPTCHA_PROVIDER"),
            "captcha_site_key": app.config.get("CAPTCHA_SITE_KEY"),
            "captcha_api_url": app.config.get("CAPTCHA_API_URL"),
        }

    # Context processor for audit
    @app.context_processor
    def inject_audit_context():
        from flask_login import current_user as cu
        if not cu.is_authenticated:
            return {
                "is_acting_as": False,
                "acting_as_user": None,
                "audit_permission_level": None,
                "audit_allowed_account_codes": None,
            }
        from app.services.audit import (
            is_acting_as_auditor, get_acting_as_user,
            get_permission_level, get_allowed_account_codes,
        )
        return {
            "is_acting_as": is_acting_as_auditor(),
            "acting_as_user": get_acting_as_user() if is_acting_as_auditor() else None,
            "audit_permission_level": get_permission_level(),
            "audit_allowed_account_codes": get_allowed_account_codes(),
        }

    # Template filter for account name masking
    @app.template_filter("mask_account")
    def mask_account_filter(account_name, account_code):
        from flask_login import current_user as cu
        if not cu.is_authenticated:
            return account_name
        from app.services.audit import get_allowed_account_codes, mask_account_name
        allowed = get_allowed_account_codes()
        return mask_account_name(account_name, account_code, allowed)

    # Serve service worker from root scope
    @app.route("/sw.js")
    def service_worker():
        from flask import send_from_directory
        return send_from_directory(app.static_folder, "sw.js",
                                   mimetype="application/javascript")

    # Security headers
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if not app.debug:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    # CLI commands
    register_cli(app)

    return app


def register_cli(app):
    @app.cli.command("seed")
    def seed_command():
        """標準勘定科目区分を初期投入する"""
        from app.services.seed import seed_account_types
        seed_account_types()
        print("勘定科目区分を投入しました。")

    @app.cli.command("seed-user")
    def seed_user_command():
        """全ユーザーに標準勘定科目を投入する"""
        from app.models.user import User
        from app.services.seed import seed_accounts_for_user
        for user in User.query.all():
            seed_accounts_for_user(user.id)
            print(f"ユーザー {user.username} に標準科目を投入しました。")

    import click

    @app.cli.command("auto-import")
    @click.option("--user-id", type=int, default=None, help="指定ユーザーのみ実行")
    @click.option("--dry-run", is_flag=True, help="DB変更なしで動作確認")
    def auto_import_command(user_id, dry_run):
        """外部ソースからレシート画像を自動取込してAI仕訳下書きを作成する"""
        from app.models.user import User
        from app.models.auto_import import AutoImportSource
        from app.services.auto_import import run_auto_import

        if user_id:
            user = User.query.get(user_id)
            if not user:
                print(f"ユーザー ID {user_id} が見つかりません。")
                return
            users = [user]
        else:
            user_ids = (
                db.session.query(AutoImportSource.user_id)
                .filter_by(is_active=True)
                .distinct()
                .all()
            )
            users = [User.query.get(uid[0]) for uid in user_ids]

        if not users:
            print("自動取込が設定されているユーザーがいません。")
            return

        prefix = "[DRY RUN] " if dry_run else ""
        for user in users:
            print(f"{prefix}自動取込: {user.username}")
            stats = run_auto_import(user.id, dry_run=dry_run)
            print(
                f"  ソース: {stats['sources_processed']}, "
                f"ファイル: {stats['files_found']}, "
                f"新規: {stats['files_new']}, "
                f"下書き: {stats['drafts_created']}, "
                f"エラー: {len(stats['errors'])}"
            )
            for err in stats["errors"][:5]:
                print(f"    ERROR: {err}")

    @app.cli.command("generate-thumbnails")
    @click.option("--dry-run", is_flag=True, help="サムネイル生成をスキップして対象件数のみ表示")
    def generate_thumbnails_command(dry_run):
        """既存画像のサムネイルを一括生成する"""
        from app.models.voucher import Voucher
        from app.models.ai_draft import AIDraft
        from app.services.storage import (
            get_storage_backend, make_thumbnail_key, generate_thumbnail,
        )

        backend = get_storage_backend()
        generated = 0
        skipped = 0
        errors = 0

        for v in Voucher.query.all():
            thumb_key = make_thumbnail_key(v.image_key)
            if backend.exists(thumb_key):
                skipped += 1
                continue
            if dry_run:
                generated += 1
                continue
            try:
                image_data = backend.get(v.image_key)
                thumb_bytes = generate_thumbnail(image_data)
                backend.put(thumb_key, thumb_bytes, "image/jpeg")
                generated += 1
            except Exception as e:
                errors += 1
                print(f"  ERROR: Voucher {v.id}: {e}")

        for d in AIDraft.query.filter(AIDraft.status.in_(["analyzed"])).all():
            thumb_key = make_thumbnail_key(d.image_key)
            if backend.exists(thumb_key):
                skipped += 1
                continue
            if dry_run:
                generated += 1
                continue
            try:
                image_data = backend.get(d.image_key)
                thumb_bytes = generate_thumbnail(image_data)
                backend.put(thumb_key, thumb_bytes, "image/jpeg")
                generated += 1
            except Exception as e:
                errors += 1
                print(f"  ERROR: Draft {d.id}: {e}")

        prefix = "[DRY RUN] " if dry_run else ""
        print(f"{prefix}サムネイル生成: {generated}件, スキップ: {skipped}件, エラー: {errors}件")
