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
    # #385: 自然移行のため nullable。werkzeug ハッシュは移行 finalize で NULL クリアし、
    # 全ユーザー移行完了後に後続マイグレで物理 DROP する (login_salt が移行済み判定)。
    password_hash = db.Column(db.String(256), nullable=True)
    user_type = db.Column(db.String(10), nullable=False, default="personal")
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    preferences = db.Column(db.JSON, nullable=True, default=dict)

    # #385 PR-T4/T4-drop: パスキー専用モード (passkey_only_login) は廃止・列 DROP 済
    # (マイグレ 073)。全ユーザーにパスワード必須 + 2FA は「Passkey or TOTP」(設計書 §3.6.6)。

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
    # is_active: 鍵未設定ユーザーのロック用 (§16.5)。SQLAlchemy ディスクリプタ
    # が UserMixin の is_active プロパティを上書きし、login_user() は DB 値を
    # 参照する (False で通常ログインを拒否)。鍵未設定ロックの解決フロー
    # (鍵設定 or 退会) では auth.login が force=True で限定セッションを張り、
    # migration_lock_gate が行動を制限する (E7 #114 PR-4b)。
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # 鍵未設定ロック (§16.5 / E7 #114 PR-4b) を付与した時刻。is_active=False に
    # した瞬間を記録し、ロック後 60 日経過で自動退会する判定 (migration-purge-locked)
    # の起点に使う。再開 (鍵設定完了) 時に NULL へ戻す。ロック中でなければ NULL。
    locked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # 一斉移行 (§16) 中の一時 MK 保管。移行完了後に NULL クリア。
    # TODO (E7 #114 実装時): このカラムへの書き込みは KMS / HSM 経由で
    # 暗号化済みバイト列のみ受け付けるバリデーションを追加する。設計書 §13.9
    # の HSM/KMS 緩和策と整合させること。
    migration_temp_mk = db.Column(db.LargeBinary, nullable=True)
    # X25519 公開鍵 (E5 #112 監査連携で使用)。MK 設定/解錠時にクライアントが
    # 鍵ペアを生成し、公開鍵を平文でここに保管する。
    public_key = db.Column(db.LargeBinary, nullable=True)
    # X25519 秘密鍵を MK で AES-GCM 暗号化した暗号文 (pkcs8) と IV (12B)。
    # サーバは平文 MK を持たないので復号できない (E5 #112 PR-A, 設計書 §14)。
    encrypted_private_key = db.Column(db.LargeBinary, nullable=True)
    private_key_iv = db.Column(db.LargeBinary, nullable=True)
    # MK ローテーション進捗 (status / progress / new_wrapped_keys_id_set 等、§10.5)
    mk_rotation_state = db.Column(db.JSON, nullable=True)

    # #385 ログイン派生 MK: 認証因子をログインパスワードに一本化する (HKDF split)。
    # 設計書 login-derived-mk.md §2 / §7.1。
    # login_server_hash = HMAC-SHA256(LOGIN_SERVER_SECRET, "login-hash"||0x00||login_verifier)
    login_server_hash = db.Column(db.LargeBinary, nullable=True)  # 32B
    login_salt = db.Column(db.LargeBinary, nullable=True)         # 16B (Argon2id per-user salt)
    login_kdf_params = db.Column(db.JSON, nullable=True)          # {memory, iterations, parallelism}
    login_secret_version = db.Column(db.SmallInteger, nullable=True)  # 遅延ローテーション用

    # #385 PR-4b-1: リカバリシードをフル復旧因子化する verifier (設計書 §3.4.1)。
    # recovery_seed_server_hash = HMAC(LOGIN_SERVER_SECRET, "recovery-hash"||0x00||
    #   recovery_verifier)。recovery_verifier = HKDF(seed_bytes,"iikanji-recovery-login-v1")。
    # DB 流出時もシード平文/verifier を得られない。旧ウィザード作成ユーザーは NULL。
    recovery_seed_server_hash = db.Column(db.LargeBinary, nullable=True)  # 32B

    # #385 PR-4b-1: セッション失効カウンタ (設計書 §3.4.1 セッション失効)。get_id() に
    # 焼き込み、リセット/パスワード変更でインクリメントすると旧 Cookie が load_user の
    # 照合で失効する。version 情報を持たない旧 Cookie は load_user が 0 とみなすため、
    # default 0 で後方互換 (既存ログインセッションを切らない)。
    session_token_version = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )

    # #385 PR-T1: TOTP 2FA (opt-in、設計書 §3.6)。secret はサーバが検証時に復号する必要が
    # あるため E2EE 不可。LOGIN_SERVER_SECRET 由来鍵で AES-256-GCM at-rest 暗号化して保管する
    # (login_derived.encrypt_totp_secret)。MK 派生には混ぜない。
    totp_secret_encrypted = db.Column(db.LargeBinary, nullable=True)  # 暗号文+tag (36B)
    totp_secret_iv = db.Column(db.LargeBinary, nullable=True)         # 12B
    # verify-before-enable: 確認コードが通るまで False (誤登録ロックアウト防止)。
    totp_enabled = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false()
    )
    totp_confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # replay 対策 (§3.6.4): 最後に検証成功した TOTP step (floor(unixtime/30)) を記録し、
    # 同一以前 step の再利用を拒否する。
    totp_last_used_step = db.Column(db.BigInteger, nullable=True)

    accounts = db.relationship("Account", backref="user", lazy="dynamic")
    journal_entries = db.relationship("JournalEntry", backref="user", lazy="dynamic")
    medical_expenses = db.relationship("MedicalExpense", backref="user", lazy="dynamic")

    @property
    def is_authenticated(self):
        """ログイン済みかどうかを is_active から切り離す (E7 #114 PR-4b)。

        Flask-Login の UserMixin は `is_authenticated` が `self.is_active` を
        返すため、鍵未設定ロック (is_active=False) のユーザーは force-login して
        もセッションが未認証扱いになり、鍵設定 API すら使えなくなる。§16.5 の
        「ロック中ユーザーがログインして鍵設定 or 退会する」フローを成立させる
        ため、認証済み判定 (= 有効なセッションを持つ) を is_active から独立させる。

        ロック中ユーザーの行動制限は migration_lock_gate (web) と各 API の
        明示的な `is_active` チェック (例: api.py の Bearer ガード) が担う。
        """
        return True

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        # #385: パスワード未設定ユーザー (password_hash NULL) は werkzeug ログインが
        # 成立しない (Passkey / リカバリ経由で入る。移行後は login_verifier で認証)。
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    @property
    def is_login_migrated(self):
        """ログイン派生 MK 方式へ移行済みか (#385)。login_salt の有無で判定。"""
        return self.login_salt is not None

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

    def get_id(self):
        """Flask-Login のセッション識別子。

        #385 PR-4b-1: セッション失効のため `session_token_version` を焼き込む
        ("id.version" 形式)。リセット/パスワード変更で version をインクリメントすると、
        旧 version を持つ Cookie は load_user の照合で弾かれ強制ログアウトになる
        (設計書 §3.4.1 セッション失効)。
        """
        return f"{self.id}.{self.session_token_version or 0}"

    def bump_session_token_version(self):
        """セッション失効: version をインクリメントし旧 Cookie を無効化する。"""
        self.session_token_version = (self.session_token_version or 0) + 1

    def __repr__(self):
        return f"<User {self.username}>"


@login_manager.user_loader
def load_user(user_id):
    # #385 PR-4b-1: get_id() は "id.version" 形式 (session_token_version 焼き込み)。
    # 旧 Cookie (version 無し) は version=0 として扱い後方互換を保つ
    # (session_token_version 既定 0 の既存ユーザーは通過する)。version 不一致は
    # リセット/パスワード変更によるセッション失効なので None を返して強制ログアウト。
    raw = str(user_id)
    if "." in raw:
        id_str, _, ver_str = raw.partition(".")
    else:
        id_str, ver_str = raw, "0"
    try:
        uid = int(id_str)
        ver = int(ver_str)
    except (TypeError, ValueError):
        return None
    user = db.session.get(User, uid)
    if user is None:
        return None
    if (user.session_token_version or 0) != ver:
        return None
    return user
