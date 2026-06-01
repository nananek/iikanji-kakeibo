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

from app.extensions import db, limiter
from app.models.audit import AuditGrant
from app.models.user import User
from app.services.api_auth import auth_required, rate_limit_key, reject_if_proxy


bp = Blueprint("keypair", __name__, url_prefix="/api/v1/keypair")

# X25519 公開鍵は raw 32B 固定。秘密鍵暗号文は pkcs8 (48B) を AES-GCM 暗号化した
# ciphertext + tag (16B) = 64B が標準。下限で空/極小ブロブを弾き、上限は
# wrapped_keys と同水準の余裕を持たせる。
PUBLIC_KEY_LEN = 32
IV_LEN = 12
MIN_ENCRYPTED_PRIVATE_KEY_SIZE = 64  # pkcs8(48B) + GCM tag(16B)
MAX_ENCRYPTED_PRIVATE_KEY_SIZE = 256


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
    proxy_err = reject_if_proxy()
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
    proxy_err = reject_if_proxy()
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


@bp.get("/<int:user_id>/public")
@auth_required(write=False)
@limiter.limit("120 per hour", key_func=rate_limit_key)
def get_other_public_key(user_id: int):
    """監査相手 (owner ⇄ auditor) の **公開鍵のみ** を返す (§14)。

    auditor が owner 宛 (AuditPackage)、owner が auditor 宛 (snapshot 暗号化や
    AuditResponse) に HPKE 暗号化するために相手の X25519 公開鍵を取得する。
    アクセス可否は「自分と相手が失効していない AuditGrant で結ばれているか」で
    判定する (IDOR 防止)。秘密鍵関連 (encrypted_private_key 等) は絶対に返さない。
    TOFU fingerprint 検証はクライアント側で行い、ここは存在確認のみ。
    """
    # 公開鍵取得も self-service。代理閲覧 (acting-as) 中は遮断する
    # (Lv3 監査者が owner として任意の相手の公開鍵を取得するのを防ぐ)。
    proxy_err = reject_if_proxy()
    if proxy_err is not None:
        return proxy_err
    me = g.auth_user.id
    # 自分が owner で相手が auditor、または自分が auditor で相手が owner の
    # 有効な grant が 1 件でもあれば取得可。
    related = AuditGrant.query.filter(
        AuditGrant.revoked_at.is_(None),
        db.or_(
            db.and_(
                AuditGrant.owner_user_id == me,
                AuditGrant.auditor_user_id == user_id,
            ),
            db.and_(
                AuditGrant.owner_user_id == user_id,
                AuditGrant.auditor_user_id == me,
            ),
        ),
    ).first()
    if related is None:
        # 関係がない相手の存在有無を秘匿するため 404
        return jsonify(error="not found"), 404

    other = db.session.get(User, user_id)
    if other is None:
        return jsonify(error="not found"), 404
    return jsonify(user_id=user_id, public_key=_b64(other.public_key))
