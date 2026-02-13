from app.views.auth import bp as auth_bp
from app.views.dashboard import bp as dashboard_bp
from app.views.cashbook import bp as cashbook_bp
from app.views.journal import bp as journal_bp
from app.views.medical import bp as medical_bp
from app.views.reports import bp as reports_bp
from app.views.accounts import bp as accounts_bp
from app.views.csv_import import bp as csv_import_bp
from app.views.webauthn import bp as webauthn_bp
from app.views.settings import bp as settings_bp


def register_blueprints(app):
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(cashbook_bp)
    app.register_blueprint(journal_bp)
    app.register_blueprint(medical_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(csv_import_bp)
    app.register_blueprint(webauthn_bp)
    app.register_blueprint(settings_bp)
