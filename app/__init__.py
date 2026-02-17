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
                "audit_allowed_account_ids": None,
            }
        from app.services.audit import (
            is_acting_as_auditor, get_acting_as_user,
            get_permission_level, get_allowed_account_ids,
        )
        return {
            "is_acting_as": is_acting_as_auditor(),
            "acting_as_user": get_acting_as_user() if is_acting_as_auditor() else None,
            "audit_permission_level": get_permission_level(),
            "audit_allowed_account_ids": get_allowed_account_ids(),
        }

    # Template filter for account name masking
    @app.template_filter("mask_account")
    def mask_account_filter(account_name, account_id):
        from flask_login import current_user as cu
        if not cu.is_authenticated:
            return account_name
        from app.services.audit import get_allowed_account_ids, mask_account_name
        allowed = get_allowed_account_ids()
        return mask_account_name(account_name, account_id, allowed)

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
