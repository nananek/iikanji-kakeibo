"""E2EE wrapped_keys API エンドポイント (E1 #108 / 設計書 §10.3, §10.9)。

設計書 §10 で確立した Master Key 管理基盤のサーバ側 API。本エンドポイントは
**暗号文の保管・取得のみ** を担当し、サーバは MK 平文を一切触らない。

エンドポイント:
- GET    /api/v1/wrapped-keys           — 自身の wrapped MK 一覧
- POST   /api/v1/wrapped-keys           — 新規登録 (暗号文受け取り)
- PUT    /api/v1/wrapped-keys/<id>/touch — last_used_at 更新
- DELETE /api/v1/wrapped-keys/<id>      — 削除 (最終要素は 409)
"""

from base64 import b64decode, b64encode
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app.extensions import db, limiter
from app.models.webauthn import WebAuthnCredential
from app.models.wrapped_key import (
    ALLOWED_METHODS,
    METHOD_PASSKEY_PRF,
    METHOD_PASSPHRASE,
    METHOD_RECOVERY_SEED,
    WrappedKey,
)


bp = Blueprint("wrapped_keys", __name__, url_prefix="/api/v1/wrapped-keys")


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
@login_required
def list_wrapped_keys():
    """自身の wrapped_keys 一覧を返す。"""
    rows = (
        WrappedKey.query
        .filter_by(user_id=current_user.id)
        .order_by(WrappedKey.id.asc())
        .all()
    )
    return jsonify(wrapped_keys=[_serialize(r) for r in rows])


@bp.post("")
@login_required
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
        return jsonify(error="wrap_iv must be 12 bytes"), 400

    webauthn_credential_id = payload.get("webauthn_credential_id")
    kdf_params = payload.get("kdf_params")
    label = payload.get("label")

    # method ごとの制約チェック (@validates でも弾かれるが、ユーザーフレンドリーな
    # メッセージのためにここでも明示的にチェック)
    if method == METHOD_PASSKEY_PRF:
        if webauthn_credential_id is None:
            return jsonify(error="passkey_prf requires webauthn_credential_id"), 400
        # 他ユーザーの credential を指定できないように所有確認
        cred = db.session.get(WebAuthnCredential, webauthn_credential_id)
        if cred is None or cred.user_id != current_user.id:
            return jsonify(error="webauthn_credential not found"), 404
    elif method in (METHOD_PASSPHRASE, METHOD_RECOVERY_SEED):
        if webauthn_credential_id is not None:
            return jsonify(
                error=f"{method} must not have webauthn_credential_id"
            ), 400

    row = WrappedKey(
        user_id=current_user.id,
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
    except Exception as exc:
        db.session.rollback()
        # UNIQUE 制約違反 (passphrase / recovery_seed の重複 or 同 credential 重複)
        return jsonify(error="conflict with existing wrapped_key"), 409

    return jsonify(_serialize(row)), 201


@bp.put("/<int:wrapped_key_id>/touch")
@login_required
# per-user で 60 req/hour、per-IP で 5000 req/hour (設計書 §10.9)
@limiter.limit("60 per hour", key_func=lambda: f"user:{current_user.id}")
@limiter.limit("5000 per hour")
def touch_wrapped_key(wrapped_key_id: int):
    """アンラップ成功時に last_used_at を更新する。

    レート制限はブルートフォース検知に使われうるため per-user 主軸 +
    per-IP 補助 (Tailscale / NAT 環境でのバースト誤検知を避ける)。
    """
    row = (
        WrappedKey.query
        .filter_by(id=wrapped_key_id, user_id=current_user.id)
        .first()
    )
    if row is None:
        return jsonify(error="not found"), 404

    row.last_used_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(_serialize(row))


@bp.delete("/<int:wrapped_key_id>")
@login_required
def delete_wrapped_key(wrapped_key_id: int):
    """wrapped_key を削除。削除後の件数が 0 になる場合は 409 Conflict。

    最後の wrapped_key を消すとアカウントが復号不能になる (鍵紛失と同等)
    ため、サーバ側でガード。クライアント UI も同じガードを持つが bypass
    可能なため二重化 (設計書 §10.4)。
    """
    row = (
        WrappedKey.query
        .filter_by(id=wrapped_key_id, user_id=current_user.id)
        .first()
    )
    if row is None:
        return jsonify(error="not found"), 404

    remaining_count = (
        WrappedKey.query
        .filter_by(user_id=current_user.id)
        .filter(WrappedKey.id != wrapped_key_id)
        .count()
    )
    if remaining_count == 0:
        return jsonify(
            error="cannot delete the last wrapped_key (account would be locked)"
        ), 409

    db.session.delete(row)
    db.session.commit()
    return "", 204
