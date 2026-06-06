from app.views.auth import bp as auth_bp
from app.views.auth_api import bp as auth_api_bp
from app.views.auth_recovery import bp as auth_recovery_bp
from app.views.dashboard import bp as dashboard_bp
from app.views.cashbook import bp as cashbook_bp
from app.views.journal import bp as journal_bp
from app.views.medical import bp as medical_bp
from app.views.reports import bp as reports_bp
from app.views.accounts import bp as accounts_bp
from app.views.csv_import import bp as csv_import_bp
from app.views.webauthn import bp as webauthn_bp
from app.views.settings import bp as settings_bp
from app.views.ai_journal import bp as ai_journal_bp
from app.views.web_import import bp as web_import_bp
from app.views.ofx_import import bp as ofx_import_bp
from app.views.auditor import bp as auditor_bp
from app.views.vouchers import bp as vouchers_bp
from app.views.api import bp as api_bp
from app.views.oauth import bp as oauth_bp
from app.views.legal import bp as legal_bp
from app.views.wrapped_keys import bp as wrapped_keys_bp
from app.views.keypair import bp as keypair_bp
from app.views.audit_packages import bp as audit_packages_bp
from app.views.ai_config_api import bp as ai_config_api_bp
from app.views.migration_lock import bp as migration_lock_bp
from app.views.admin import bp as admin_bp


def register_blueprints(app):
    app.register_blueprint(auth_bp)
    app.register_blueprint(auth_api_bp)
    app.register_blueprint(auth_recovery_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(cashbook_bp)
    app.register_blueprint(journal_bp)
    app.register_blueprint(medical_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(csv_import_bp)
    app.register_blueprint(web_import_bp)
    app.register_blueprint(ofx_import_bp)
    app.register_blueprint(webauthn_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(ai_journal_bp)
    app.register_blueprint(auditor_bp)
    app.register_blueprint(vouchers_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(oauth_bp)
    app.register_blueprint(legal_bp)
    app.register_blueprint(wrapped_keys_bp)
    app.register_blueprint(keypair_bp)
    app.register_blueprint(audit_packages_bp)
    app.register_blueprint(ai_config_api_bp)
    app.register_blueprint(migration_lock_bp)
    app.register_blueprint(admin_bp)
