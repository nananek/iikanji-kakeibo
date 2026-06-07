from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db, login_manager


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    user_type = db.Column(db.String(10), nullable=False, default="personal")
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    preferences = db.Column(db.JSON, nullable=True, default=dict)

    # 利用規約・プライバシーポリシーへの同意バージョン (YYYY-MM-DD 形式)。
    # 規約改訂時に CURRENT_TERMS_VERSION が更新され、ユーザーの値と
    # 一致しない場合は再同意フローへ誘導する (Phase 1 #66)。
    accepted_terms_version = db.Column(db.String(20), nullable=True)
    # 直近の quota 警告通知レベル (Phase 6 #71)。
    # NULL: 未送信 or 70% 未満まで回復済 / "warning": 80% 到達済 /
    # "critical": 95% 到達済。同じ帯にいる間はメール再送しない (重複防止)。
    last_quota_warning_level = db.Column(db.String(20), nullable=True)

    # TOTP 2要素認証（opt-in）。secret は SECRET_KEY 由来の Fernet で暗号化。
    # totp_last_used_step はログイン時の同一コード再利用 (リプレイ) 防止用。
    totp_secret_encrypted = db.Column(db.LargeBinary, nullable=True)
    totp_enabled = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false()
    )
    totp_confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    totp_last_used_step = db.Column(db.BigInteger, nullable=True)

    accounts = db.relationship("Account", backref="user", lazy="dynamic")
    journal_entries = db.relationship("JournalEntry", backref="user", lazy="dynamic")
    medical_expenses = db.relationship("MedicalExpense", backref="user", lazy="dynamic")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_pref(self, key, default=None):
        if not self.preferences:
            return default
        return self.preferences.get(key, default)

    def set_pref(self, key, value):
        if not self.preferences:
            self.preferences = {}
        prefs = dict(self.preferences)
        prefs[key] = value
        self.preferences = prefs

    @property
    def has_totp(self):
        # totp_enabled は nullable=False / default=False のため None にならない
        return self.totp_enabled

    def __repr__(self):
        return f"<User {self.username}>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))
