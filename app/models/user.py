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

    # 利用規約・プライバシーポリシーへの同意バージョン (YYYY-MM-DD 形式)。
    # 規約改訂時に CURRENT_TERMS_VERSION が更新され、ユーザーの値と
    # 一致しない場合は再同意フローへ誘導する (Phase 1 #66)。
    accepted_terms_version = db.Column(db.String(20), nullable=True)
    # 直近の quota 警告通知レベル (Phase 6 #71)。
    # NULL: 未送信 or 70% 未満まで回復済 / "warning": 80% 到達済 /
    # "critical": 95% 到達済。同じ帯にいる間はメール再送しない (重複防止)。
    last_quota_warning_level = db.Column(db.String(20), nullable=True)
    # リカバリコード（パスキー紛失時の非常用、1 回限り使用）
    recovery_code_hash = db.Column(db.String(64), nullable=True)
    recovery_code_prefix = db.Column(db.String(20), nullable=True)
    recovery_code_created_at = db.Column(db.DateTime(timezone=True), nullable=True)
    recovery_code_used_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # E2EE Phase E1 (#108): 鍵管理基盤関連カラム。設計書 §10 / §16 参照。
    # is_active: 鍵未設定ユーザーのロック用 (Flask-Login の UserMixin は default True を
    # 返すが、DB レベルで管理することで永続的なロック状態を保持できる)。
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # 一斉移行 (§16) 中の一時 MK 保管。移行完了後に NULL クリア。
    migration_temp_mk = db.Column(db.LargeBinary, nullable=True)
    # X25519 公開鍵 (E5 #112 監査連携で使用)。新規登録時に自動生成。
    public_key = db.Column(db.LargeBinary, nullable=True)
    # MK ローテーション進捗 (status / progress / new_wrapped_keys_id_set 等、§10.5)
    mk_rotation_state = db.Column(db.JSON, nullable=True)

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
