from flask import Flask

from app.config import Config
from app.extensions import db, migrate, login_manager, csrf


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Models (import so Alembic can detect them)
    from app import models  # noqa: F401

    # Blueprints
    from app.views import register_blueprints
    register_blueprints(app)

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
