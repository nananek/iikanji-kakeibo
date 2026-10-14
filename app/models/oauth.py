"""OAuth 2.0 Device Authorization Grant (RFC 8628) モデル

および MCP (Model Context Protocol) クライアント向けの OAuth 2.1
Authorization Code + PKCE / 動的クライアント登録 (RFC 7591) モデル。
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from app.extensions import db

# device_code / user_code の有効時間
DEVICE_CODE_EXPIRES_IN = 600  # 10分
# クライアントが /oauth/token をポーリングする推奨間隔 (秒)
DEVICE_CODE_POLL_INTERVAL = 5

# Authorization Code + PKCE の有効時間 (秒)。短命にしてリプレイを防ぐ。
AUTHORIZATION_CODE_EXPIRES_IN = 60
# 発行するアクセストークン / リフレッシュトークンの有効時間 (秒)。
ACCESS_TOKEN_EXPIRES_IN = 3600          # 1時間
REFRESH_TOKEN_EXPIRES_IN = 30 * 24 * 3600  # 30日

CLIENT_ID_PREFIX = "ikc_"

# user_code に使用する文字（混同を避けるため I, O, 0, 1 を除外）
_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

OAUTH_TOKEN_PREFIX = "ikt_"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    """SQLite が返す naive datetime を UTC aware として正規化する。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class OAuthDevice(db.Model):
    """Device Authorization Grant のセッション記録"""

    __tablename__ = "oauth_devices"

    id = db.Column(db.Integer, primary_key=True)
    device_code_hash = db.Column(
        db.String(64), nullable=False, unique=True, index=True
    )
    user_code = db.Column(db.String(16), nullable=False, unique=True, index=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    status = db.Column(
        db.String(20), nullable=False, default="pending"
    )  # pending / approved / denied / expired / consumed
    client_name = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now_utc)
    expires_at = db.Column(db.DateTime, nullable=False)
    last_polled_at = db.Column(db.DateTime, nullable=True)
    read_only = db.Column(db.Boolean, nullable=False, default=False)
    # デバイス承認時に選択されたスコープ (カンマ区切り)。NULL は全スコープ。
    scopes = db.Column(db.String(500), nullable=True)

    user = db.relationship(
        "User", backref=db.backref("oauth_devices", lazy="dynamic")
    )

    @staticmethod
    def generate_user_code() -> str:
        """ユーザー入力用の8文字コード（XXXX-XXXX形式）を生成"""
        chars = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
        return f"{chars[:4]}-{chars[4:]}"

    @staticmethod
    def generate_device_code() -> str:
        """device_code (raw) を生成"""
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash_device_code(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def create_pending(cls, client_name: str | None = None):
        """新しい pending セッションを作成し (raw_device_code, OAuthDevice) を返す"""
        for _ in range(8):
            user_code = cls.generate_user_code()
            existing = cls.query.filter_by(user_code=user_code).first()
            if not existing:
                break
        else:
            raise RuntimeError("user_code の生成に失敗しました")

        raw_device = cls.generate_device_code()
        device = cls(
            device_code_hash=cls.hash_device_code(raw_device),
            user_code=user_code,
            status="pending",
            client_name=(client_name or "")[:100] or None,
            expires_at=_now_utc() + timedelta(seconds=DEVICE_CODE_EXPIRES_IN),
        )
        return raw_device, device

    def is_expired(self) -> bool:
        expires = _as_aware(self.expires_at)
        return expires is not None and _now_utc() >= expires


class OAuthToken(db.Model):
    """OAuth Device Flow で発行されたアクセストークン"""

    __tablename__ = "oauth_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    token_prefix = db.Column(db.String(20), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now_utc)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    read_only = db.Column(db.Boolean, nullable=False, default=False)

    # --- MCP / Authorization Code フロー用の追加属性 ---
    # 付与スコープ (カンマ区切り)。NULL は従来の Device Flow 互換で
    # 「全スコープ」を意味する。"" はスコープなし。
    scopes = db.Column(db.String(500), nullable=True)
    # アクセストークンの有効期限。NULL は無期限 (従来トークン)。
    expires_at = db.Column(db.DateTime, nullable=True)
    # 発行元クライアント (Authorization Code フローのみ)。
    client_id = db.Column(db.String(64), nullable=True)
    # リフレッシュトークン (ハッシュのみ保存)。
    refresh_token_hash = db.Column(
        db.String(64), nullable=True, unique=True, index=True
    )
    refresh_token_prefix = db.Column(db.String(20), nullable=True)
    refresh_expires_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship(
        "User", backref=db.backref("oauth_tokens", lazy="dynamic")
    )

    @staticmethod
    def generate():
        """アクセストークンを生成し (raw, hash, prefix) を返す"""
        raw = OAUTH_TOKEN_PREFIX + secrets.token_hex(32)
        h = hashlib.sha256(raw.encode()).hexdigest()
        prefix = raw[:11] + "..."
        return raw, h, prefix

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def generate_refresh():
        """リフレッシュトークンを生成し (raw, hash, prefix) を返す"""
        raw = "ikr_" + secrets.token_hex(32)
        h = hashlib.sha256(raw.encode()).hexdigest()
        prefix = raw[:11] + "..."
        return raw, h, prefix

    @classmethod
    def issue(cls, *, user_id, name, scopes, read_only, client_id=None,
              access_expires_in=ACCESS_TOKEN_EXPIRES_IN,
              with_refresh=True):
        """Authorization Code フロー用にトークン一式を発行して永続化する。

        Returns:
            (raw_access, raw_refresh or None, OAuthToken)
        """
        raw, h, prefix = cls.generate()
        now = _now_utc()
        token = cls(
            user_id=user_id,
            name=(name or "MCP クライアント")[:100],
            token_hash=h,
            token_prefix=prefix,
            scopes=_serialize_scopes(scopes),
            read_only=read_only,
            client_id=client_id,
            expires_at=(now + timedelta(seconds=access_expires_in)
                        if access_expires_in else None),
        )
        raw_refresh = None
        if with_refresh:
            raw_refresh, rh, rprefix = cls.generate_refresh()
            token.refresh_token_hash = rh
            token.refresh_token_prefix = rprefix
            token.refresh_expires_at = now + timedelta(
                seconds=REFRESH_TOKEN_EXPIRES_IN
            )
        db.session.add(token)
        return raw, raw_refresh, token

    def rotate_refresh(self):
        """リフレッシュトークンをローテートし新しい raw を返す。"""
        raw, rh, prefix = self.generate_refresh()
        self.refresh_token_hash = rh
        self.refresh_token_prefix = prefix
        self.refresh_expires_at = _now_utc() + timedelta(
            seconds=REFRESH_TOKEN_EXPIRES_IN
        )
        return raw

    def scope_set(self):
        """付与スコープの集合。None は「全スコープ (従来互換)」を意味する。"""
        if self.scopes is None:
            return None
        return {s for s in self.scopes.split(",") if s}

    def has_scope(self, scope: str) -> bool:
        s = self.scope_set()
        return s is None or scope in s

    def is_expired(self) -> bool:
        expires = _as_aware(self.expires_at)
        return expires is not None and _now_utc() >= expires

    def refresh_is_valid(self) -> bool:
        if not self.refresh_token_hash or not self.is_active:
            return False
        expires = _as_aware(self.refresh_expires_at)
        return expires is None or _now_utc() < expires


def _serialize_scopes(scopes) -> str | None:
    """スコープ集合を保存用文字列へ。None は「全スコープ」を維持する。"""
    if scopes is None:
        return None
    return ",".join(sorted(set(scopes)))


class OAuthClient(db.Model):
    """動的クライアント登録 (RFC 7591) された MCP クライアント"""

    __tablename__ = "oauth_clients"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    client_secret_hash = db.Column(db.String(64), nullable=True)
    client_name = db.Column(db.String(200), nullable=True)
    # 登録されたリダイレクト URI (JSON 配列文字列)
    redirect_uris_json = db.Column(db.Text, nullable=False, default="[]")
    grant_types = db.Column(
        db.String(200), nullable=False, default="authorization_code refresh_token"
    )
    response_types = db.Column(db.String(100), nullable=False, default="code")
    token_endpoint_auth_method = db.Column(
        db.String(50), nullable=False, default="none"
    )
    scope = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now_utc)
    last_used_at = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def generate_client_id() -> str:
        return CLIENT_ID_PREFIX + secrets.token_urlsafe(24)

    @staticmethod
    def hash_secret(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def redirect_uri_list(self) -> list[str]:
        try:
            data = json.loads(self.redirect_uris_json or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return [u for u in data if isinstance(u, str)]

    def allows_redirect_uri(self, uri: str) -> bool:
        """登録済み URI と完全一致のみ許可する (オープンリダイレクト対策)。"""
        return uri in self.redirect_uri_list


class OAuthAuthorizationCode(db.Model):
    """Authorization Code + PKCE の短命な認可コード"""

    __tablename__ = "oauth_authorization_codes"

    id = db.Column(db.Integer, primary_key=True)
    code_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    client_id = db.Column(db.String(64), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    redirect_uri = db.Column(db.String(1000), nullable=False)
    scopes = db.Column(db.String(500), nullable=True)
    code_challenge = db.Column(db.String(200), nullable=False)
    code_challenge_method = db.Column(db.String(10), nullable=False, default="S256")
    resource = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now_utc)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def generate() -> tuple[str, str]:
        raw = secrets.token_urlsafe(40)
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def hash_code(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    def is_expired(self) -> bool:
        expires = _as_aware(self.expires_at)
        return expires is not None and _now_utc() >= expires

    def is_usable(self) -> bool:
        return self.used_at is None and not self.is_expired()

