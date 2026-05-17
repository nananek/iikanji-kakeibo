import hashlib
import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db, login_manager


RECOVERY_CODE_PREFIX = "ikr_"
_RECOVERY_PREFIX_LEN = 11  # "ikr_" + 7 chars hex


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

    # パスキー専用ログインモード（オプトイン、デフォルト無効）
    passkey_only_login = db.Column(db.Boolean, nullable=False, default=False)
    # リカバリコード（パスキー紛失時の非常用、1 回限り使用）
    recovery_code_hash = db.Column(db.String(64), nullable=True)
    recovery_code_prefix = db.Column(db.String(20), nullable=True)
    recovery_code_created_at = db.Column(db.DateTime(timezone=True), nullable=True)
    recovery_code_used_at = db.Column(db.DateTime(timezone=True), nullable=True)

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

    # --- リカバリコード ---

    def set_recovery_code(self):
        """新しいリカバリコードを生成し、ハッシュを保存。生コードを返す。

        既存コードがあれば即時無効化（上書き）。生コードは呼び出し側で
        1 回だけユーザーに表示し、DB には SHA-256 ハッシュのみ保存する。
        """
        raw = RECOVERY_CODE_PREFIX + secrets.token_hex(32)
        self.recovery_code_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.recovery_code_prefix = raw[:_RECOVERY_PREFIX_LEN] + "..."
        self.recovery_code_created_at = datetime.now(timezone.utc)
        self.recovery_code_used_at = None
        return raw

    def verify_recovery_code(self, raw):
        """リカバリコードが正しく、まだ使用されていないか検証する。"""
        if not self.recovery_code_hash or self.recovery_code_used_at is not None:
            return False
        if not isinstance(raw, str) or not raw:
            return False
        candidate = hashlib.sha256(raw.encode()).hexdigest()
        return secrets.compare_digest(candidate, self.recovery_code_hash)

    def consume_recovery_code(self):
        """リカバリコードを使用済みとしてマークする。"""
        self.recovery_code_used_at = datetime.now(timezone.utc)

    @property
    def has_active_recovery_code(self):
        return (
            self.recovery_code_hash is not None
            and self.recovery_code_used_at is None
        )

    def __repr__(self):
        return f"<User {self.username}>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))
