"""E2EE X25519 鍵ペア API (E5 #112 PR-A / 設計書 §14)。

監査連携で owner / auditor が相手の公開鍵宛にスナップショットを HPKE 暗号化する
ための鍵ペアを保管する。`wrapped_keys` と同じく **暗号文の保管・取得のみ** を担い、
サーバは MK 平文も X25519 秘密鍵平文も一切触らない。

- GET /api/v1/keypair  — 自身の {public_key, encrypted_private_key, private_key_iv}
- PUT /api/v1/keypair  — 鍵ペアを保存 (public_key 設定済みなら 409。回転は E5 後続)

他ユーザーの公開鍵取得 (auditor → owner 宛に暗号化する用途) は PR-C の
audit_packages API 側に置く (AuditGrant ベースのアクセス制御を伴うため)。
"""

from base64 import b64decode, b64encode

from flask import Blueprint, g, jsonify, request
from flask_login import current_user

from app.extensions import db, limiter
from app.services.api_auth import auth_required, rate_limit_key


bp = Blueprint("keypair", __name__, url_prefix="/api/v1/keypair")

# X25519 公開鍵は raw 32B 固定。秘密鍵暗号文は pkcs8 (48B) を AES-GCM 暗号化した
# ciphertext + tag (16B) = 64B が標準。下限で空/極小ブロブを弾き、上限は
# wrapped_keys と同水準の余裕を持たせる。
PUBLIC_KEY_LEN = 32
IV_LEN = 12
MIN_ENCRYPTED_PRIVATE_KEY_SIZE = 64  # pkcs8(48B) + GCM tag(16B)
MAX_ENCRYPTED_PRIVATE_KEY_SIZE = 256


def _reject_if_proxy():
    """監査代理閲覧 (acting-as) 中のアクセスを拒否する。

    鍵ペア管理は常にログイン中の本人に対する self-service。`auth_required`
    (resolve_bearer_or_session) は Lv3 代理閲覧時に `g.auth_user = owner` を
    返すため、そのまま PUT すると Lv3 監査者がオーナーの公開鍵を書き込めて
    しまう (鍵注入攻撃)。ここで本人 (current_user) と effective user の不一致を
    検知して 403 で遮断する。Bearer 認証時は current_user 非認証なので対象外
    (代理閲覧の概念がない)。
    """
    if (
        current_user.is_authenticated
        and g.auth_user.id != current_user.id
    ):
        return jsonify(
            error="keypair management is not available during proxy view"
        ), 403
    return None


def _b64(b: bytes | None) -> str | None:
    return b64encode(b).decode("ascii") if b is not None else None


def _b64_or_400(payload: dict, key: str) -> tuple[bytes | None, str | None]:
    """payload[key] を base64 デコード。失敗時は固定エラーメッセージを返す。

    wrapped_keys.py の同名ヘルパーと同方針 (CodeQL stack-trace-exposure 回避の
    ため例外詳細は捨て、固定文字列のみ返す)。
    """
    val = payload.get(key)
    if val is None:
        return None, f"{key} is required"
    if not isinstance(val, str):
        return None, f"{key} must be a base64 string"
    try:
        return b64decode(val, validate=True), None
    except Exception:
        return None, f"{key} is not valid base64"


@bp.get("")
@auth_required(write=False)
@limiter.limit("120 per hour", key_func=rate_limit_key)
def get_keypair():
    """自身の鍵ペア (公開鍵 + MK ラップ秘密鍵) を返す。未設定なら各 null。"""
    proxy_err = _reject_if_proxy()
    if proxy_err is not None:
        return proxy_err
    user = g.auth_user
    return jsonify(
        public_key=_b64(user.public_key),
        encrypted_private_key=_b64(user.encrypted_private_key),
        private_key_iv=_b64(user.private_key_iv),
    )


@bp.put("")
@auth_required(write=True)
@limiter.limit("20 per hour", key_func=rate_limit_key)
def put_keypair():
    """X25519 鍵ペアを保存する。

    public_key が既に設定済みの場合は 409 (鍵ペアの不可逆性を守る。鍵回転は
    監査パッケージの再暗号化を伴うため E5 後続スコープ)。
    """
    proxy_err = _reject_if_proxy()
    if proxy_err is not None:
        return proxy_err
    user = g.auth_user
    if user.public_key is not None:
        return jsonify(error="keypair already set"), 409

    payload = request.get_json(silent=True) or {}

    public_key, err = _b64_or_400(payload, "public_key")
    if err is not None:
        return jsonify(error=err), 400
    encrypted_private_key, err = _b64_or_400(payload, "encrypted_private_key")
    if err is not None:
        return jsonify(error=err), 400
    private_key_iv, err = _b64_or_400(payload, "private_key_iv")
    if err is not None:
        return jsonify(error=err), 400

    if len(public_key) != PUBLIC_KEY_LEN:
        return jsonify(error=f"public_key must be {PUBLIC_KEY_LEN} bytes"), 400
    if len(private_key_iv) != IV_LEN:
        return jsonify(error=f"private_key_iv must be {IV_LEN} bytes"), 400
    if not encrypted_private_key:
        return jsonify(error="encrypted_private_key is required"), 400
    if len(encrypted_private_key) < MIN_ENCRYPTED_PRIVATE_KEY_SIZE:
        return jsonify(
            error=(
                "encrypted_private_key too small "
                f"(min {MIN_ENCRYPTED_PRIVATE_KEY_SIZE} bytes)"
            )
        ), 400
    if len(encrypted_private_key) > MAX_ENCRYPTED_PRIVATE_KEY_SIZE:
        return jsonify(
            error=(
                "encrypted_private_key too large "
                f"(max {MAX_ENCRYPTED_PRIVATE_KEY_SIZE} bytes)"
            )
        ), 400

    user.public_key = public_key
    user.encrypted_private_key = encrypted_private_key
    user.private_key_iv = private_key_iv
    db.session.commit()
    return jsonify(
        public_key=_b64(user.public_key),
        encrypted_private_key=_b64(user.encrypted_private_key),
        private_key_iv=_b64(user.private_key_iv),
    ), 200
