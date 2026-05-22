"""E2EE wrapped_keys API エンドポイント (E1 #108 / 設計書 §10.3, §10.9)。

設計書 §10 で確立した Master Key 管理基盤のサーバ側 API。本エンドポイントは
**暗号文の保管・取得のみ** を担当し、サーバは MK 平文を一切触らない。

エンドポイント:
- GET    /api/v1/wrapped-keys           — 自身の wrapped MK 一覧
- POST   /api/v1/wrapped-keys           — 新規登録 (暗号文受け取り)
- PUT    /api/v1/wrapped-keys/<id>/touch — last_used_at 更新
- DELETE /api/v1/wrapped-keys/<id>      — 削除 (最終要素は 409)
"""

import functools
from base64 import b64decode, b64encode
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db, limiter
from app.models.api_key import APIKey
from app.models.oauth import OAuthToken
from app.models.user import User
from app.models.webauthn import WebAuthnCredential
from app.models.wrapped_key import (
    ALLOWED_METHODS,
    METHOD_PASSKEY_PRF,
    METHOD_PASSPHRASE,
    METHOD_RECOVERY_SEED,
    WrappedKey,
)


bp = Blueprint("wrapped_keys", __name__, url_prefix="/api/v1/wrapped-keys")

# サイズ上限。AES-256-GCM wrapped MK は 48B (= 32B 鍵 + 16B tag) 想定。
# label は users が UI で識別する短い文字列 (例: "iPhone 14 Pro Passkey")。
MAX_WRAPPED_KEY_SIZE = 256
MAX_LABEL_LENGTH = 100

# Argon2id パラメータの許容範囲 (passphrase method の弱パラメータ DoS 防止、
# 設計書 §10.1 の推奨値 memory=65536, iterations=3, parallelism=1 を中心に
# 余裕を持たせた範囲)。
KDF_MEMORY_MIN, KDF_MEMORY_MAX = 8192, 1048576       # 8MiB - 1GiB (KiB 単位)
KDF_ITERATIONS_MIN, KDF_ITERATIONS_MAX = 1, 16
KDF_PARALLELISM_MIN, KDF_PARALLELISM_MAX = 1, 8


def _resolve_auth(write: bool) -> tuple[User | None, tuple | None]:
    """Bearer or Web session 認証を解決して User を返す。

    - Authorization: Bearer <ikt_...> → OAuthToken (read_only なら write 拒否)
    - Authorization: Bearer <ik_...>  → APIKey
    - ヘッダなし → Flask-Login のセッション
    戻り値: (user, error_response or None)
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        raw = auth[7:]
        now = datetime.now(timezone.utc)
        # OAuth Device Flow Token
        if raw.startswith("ikt_"):
            token_hash = OAuthToken.hash_token(raw)
            token = OAuthToken.query.filter_by(
                token_hash=token_hash, is_active=True
            ).first()
            if not token:
                return None, (jsonify(error="Invalid token"), 401)
            if write and token.read_only:
                return None, (jsonify(error="read-only token"), 403)
            token.last_used_at = now
            db.session.commit()
            user = db.session.get(User, token.user_id)
            if user is None:
                return None, (jsonify(error="User not found"), 401)
            return user, None
        # 従来の APIKey
        key_hash = APIKey.hash_key(raw)
        api_key = APIKey.query.filter_by(
            key_hash=key_hash, is_active=True
        ).first()
        if not api_key:
            return None, (jsonify(error="Invalid API key"), 401)
        api_key.last_used_at = now
        db.session.commit()
        user = db.session.get(User, api_key.user_id)
        if user is None:
            return None, (jsonify(error="User not found"), 401)
        return user, None

    # Web セッション
    if current_user.is_authenticated:
        return current_user._get_current_object(), None
    return None, (jsonify(error="Authentication required"), 401)


def auth_required(write: bool = False):
    """Bearer + Web セッションの両方を受け入れる統合デコレータ。

    認証成功時、`g.auth_user` に User をセット。エンドポイントは
    `g.auth_user.id` を使って自身のリソースをフィルタする。
    write=True なら OAuth read-only トークンを拒否 (403)。
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            user, err = _resolve_auth(write=write)
            if err is not None:
                return err
            g.auth_user = user
            return f(*args, **kwargs)
        return wrapper
    return decorator


def _rate_limit_key() -> str:
    """auth_user が解決された後にレート制限の per-user キーとして使う。"""
    user = getattr(g, "auth_user", None)
    if user is not None:
        return f"user:{user.id}"
    # Fallback (auth_required 未通過時): IP ベース
    return request.remote_addr or "anonymous"


def _b64(b: bytes | None) -> str | None:
    return b64encode(b).decode("ascii") if b is not None else None


def _serialize(row: WrappedKey) -> dict:
    """WrappedKey をクライアント返却用 dict に変換。暗号文は base64 で出す。"""
    return {
        "id": row.id,
        "method": row.method,
        "webauthn_credential_id": row.webauthn_credential_id,
        "wrapped_master_key": _b64(row.wrapped_master_key),
        "wrap_iv": _b64(row.wrap_iv),
        "salt": _b64(row.salt),
        "kdf_params": row.kdf_params,
        "label": row.label,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": (
            row.last_used_at.isoformat() if row.last_used_at else None
        ),
    }


def _b64_or_400(payload: dict, key: str, *, required: bool = True) -> bytes | None:
    """payload[key] を base64 デコード。エラー時は 400 を発生させるための例外送出。"""
    val = payload.get(key)
    if val is None:
        if required:
            raise ValueError(f"{key} is required")
        return None
    if not isinstance(val, str):
        raise ValueError(f"{key} must be a base64 string")
    try:
        return b64decode(val, validate=True)
    except Exception as exc:
        raise ValueError(f"{key} is not valid base64") from exc


@bp.get("")
@auth_required(write=False)
@limiter.limit("120 per hour", key_func=_rate_limit_key)
def list_wrapped_keys():
    """自身の wrapped_keys 一覧を返す。"""
    rows = (
        WrappedKey.query
        .filter_by(user_id=g.auth_user.id)
        .order_by(WrappedKey.id.asc())
        .all()
    )
    return jsonify(wrapped_keys=[_serialize(r) for r in rows])


def _is_int(x) -> bool:
    """JSON の True/False が int として通り抜けるのを防ぐ厳格チェック。"""
    return isinstance(x, int) and not isinstance(x, bool)


def _validate_kdf_params(kdf_params: dict) -> str | None:
    """Argon2id パラメータの範囲チェック。エラー時は文字列を返す。"""
    mem = kdf_params.get("memory")
    itr = kdf_params.get("iterations")
    par = kdf_params.get("parallelism")
    if not _is_int(mem) or not (KDF_MEMORY_MIN <= mem <= KDF_MEMORY_MAX):
        return f"kdf_params.memory must be int {KDF_MEMORY_MIN}..{KDF_MEMORY_MAX} (KiB)"
    if not _is_int(itr) or not (KDF_ITERATIONS_MIN <= itr <= KDF_ITERATIONS_MAX):
        return f"kdf_params.iterations must be int {KDF_ITERATIONS_MIN}..{KDF_ITERATIONS_MAX}"
    if not _is_int(par) or not (KDF_PARALLELISM_MIN <= par <= KDF_PARALLELISM_MAX):
        return f"kdf_params.parallelism must be int {KDF_PARALLELISM_MIN}..{KDF_PARALLELISM_MAX}"
    return None


@bp.post("")
@auth_required(write=True)
@limiter.limit("20 per hour", key_func=_rate_limit_key)
def create_wrapped_key():
    """新規 wrapped MK を登録。サーバは復号しない。"""
    payload = request.get_json(silent=True) or {}

    method = payload.get("method")
    if method not in ALLOWED_METHODS:
        return jsonify(error=f"method must be one of {list(ALLOWED_METHODS)}"), 400

    try:
        wrapped = _b64_or_400(payload, "wrapped_master_key")
        wrap_iv = _b64_or_400(payload, "wrap_iv")
        salt = _b64_or_400(payload, "salt", required=False)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    if not wrap_iv or len(wrap_iv) != 12:
        return jsonify(error="IV length must be 12 bytes"), 400

    if len(wrapped) > MAX_WRAPPED_KEY_SIZE:
        return jsonify(
            error=f"wrapped_master_key too large (max {MAX_WRAPPED_KEY_SIZE} bytes)"
        ), 400

    webauthn_credential_id = payload.get("webauthn_credential_id")
    kdf_params = payload.get("kdf_params")
    label = payload.get("label")

    if label is not None:
        if not isinstance(label, str) or len(label) > MAX_LABEL_LENGTH:
            return jsonify(
                error=f"label must be a string of at most {MAX_LABEL_LENGTH} characters"
            ), 400

    # 型検証: JSON で文字列等を渡されたら 400 で弾く (SQLAlchemy 例外で 500 に
    # ならないように)。bool が int として通り抜けるのを防ぐため _is_int 使用
    if webauthn_credential_id is not None and not _is_int(webauthn_credential_id):
        return jsonify(error="webauthn_credential_id must be int"), 400

    # method ごとの制約チェック (@validates でも弾かれるが、ユーザーフレンドリーな
    # メッセージのためにここでも明示的にチェック)
    if method == METHOD_PASSKEY_PRF:
        if webauthn_credential_id is None:
            return jsonify(error="passkey_prf requires webauthn_credential_id"), 400
        # 他ユーザーの credential を指定できないように所有確認
        cred = db.session.get(WebAuthnCredential, webauthn_credential_id)
        if cred is None or cred.user_id != g.auth_user.id:
            return jsonify(error="webauthn_credential not found"), 404
    elif method == METHOD_PASSPHRASE:
        if webauthn_credential_id is not None:
            return jsonify(
                error=f"{method} must not have webauthn_credential_id"
            ), 400
        # passphrase は Argon2id KDF を使うため salt + kdf_params 必須
        # (設計書 §10.1: 弱い鍵派生を防ぐ)
        if salt is None:
            return jsonify(error="passphrase requires salt"), 400
        if not isinstance(kdf_params, dict):
            return jsonify(error="passphrase requires kdf_params dict"), 400
        # 極端な Argon2 パラメータでクライアントを DoS させないようサーバ側で
        # 範囲を強制 (E2EE 下ではサーバが復号できないので "信頼すべき" 範囲を
        # 設計書 §10.1 に明示)
        kdf_err = _validate_kdf_params(kdf_params)
        if kdf_err:
            return jsonify(error=kdf_err), 400
    elif method == METHOD_RECOVERY_SEED:
        if webauthn_credential_id is not None:
            return jsonify(
                error=f"{method} must not have webauthn_credential_id"
            ), 400

    row = WrappedKey(
        user_id=g.auth_user.id,
        method=method,
        webauthn_credential_id=webauthn_credential_id,
        wrapped_master_key=wrapped,
        wrap_iv=wrap_iv,
        salt=salt,
        kdf_params=kdf_params,
        label=label,
    )
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # UNIQUE 制約違反 (passphrase / recovery_seed の重複 or 同 credential 重複)
        return jsonify(error="conflict with existing wrapped_key"), 409
    # IntegrityError 以外は 500 として Flask に処理させる (DB 接続エラー等)

    return jsonify(_serialize(row)), 201


@bp.put("/<int:wrapped_key_id>/touch")
@auth_required(write=True)
# per-user で 60 req/hour、per-IP で 5000 req/hour (設計書 §10.9)
@limiter.limit("60 per hour", key_func=_rate_limit_key)
@limiter.limit("5000 per hour")
def touch_wrapped_key(wrapped_key_id: int):
    """アンラップ成功時に last_used_at を更新する。

    レート制限はブルートフォース検知に使われうるため per-user 主軸 +
    per-IP 補助 (Tailscale / NAT 環境でのバースト誤検知を避ける)。
    """
    row = (
        WrappedKey.query
        .filter_by(id=wrapped_key_id, user_id=g.auth_user.id)
        .first()
    )
    if row is None:
        return jsonify(error="not found"), 404

    row.last_used_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(_serialize(row))


@bp.delete("/<int:wrapped_key_id>")
@auth_required(write=True)
@limiter.limit("20 per hour", key_func=_rate_limit_key)
def delete_wrapped_key(wrapped_key_id: int):
    """wrapped_key を削除。削除後の件数が 0 になる場合は 409 Conflict。

    最後の wrapped_key を消すとアカウントが復号不能になる (鍵紛失と同等)
    ため、サーバ側でガード。クライアント UI も同じガードを持つが bypass
    可能なため二重化 (設計書 §10.4)。
    """
    row = (
        WrappedKey.query
        .filter_by(id=wrapped_key_id, user_id=g.auth_user.id)
        .first()
    )
    if row is None:
        return jsonify(error="not found"), 404

    # TOCTOU 軽減: 削除を先に flush (トランザクション内のみ確定) → 残件数を
    # チェック → 0 件なら rollback。並行 DELETE がほぼ同時に 2 件削除しようと
    # した場合でも、両者がそれぞれ rollback されて 1 件以上残る (片方が勝つ
    # ケースもあるが、いずれも「最後の鍵」になる前に防がれる)。
    # PostgreSQL 本番では SELECT ... FOR UPDATE で行ロックを取るのが望ましい
    # (E1 PR-D で改善)。
    db.session.delete(row)
    db.session.flush()
    remaining_count = (
        WrappedKey.query
        .filter_by(user_id=g.auth_user.id)
        .count()
    )
    if remaining_count == 0:
        db.session.rollback()
        return jsonify(
            error="cannot delete the last wrapped_key (account would be locked)"
        ), 409
    db.session.commit()
    return "", 204
