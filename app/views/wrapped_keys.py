"""E2EE wrapped_keys API エンドポイント (E1 #108 / 設計書 §10.3-10.5, §10.9)。

設計書 §10 で確立した Master Key 管理基盤のサーバ側 API。本エンドポイントは
**暗号文の保管・取得のみ** を担当し、サーバは MK 平文を一切触らない。

エンドポイント:
- GET    /api/v1/wrapped-keys                — 自身の wrapped MK 一覧
- POST   /api/v1/wrapped-keys                — 新規登録 (暗号文受け取り)
                                                X-Rotation-Id ヘッダがあれば
                                                ローテーション中の new_wrapped_keys
                                                として state に記録
- PUT    /api/v1/wrapped-keys/<id>/touch     — last_used_at 更新
- DELETE /api/v1/wrapped-keys/<id>           — 削除 (最終要素は 409)
- POST   /api/v1/wrapped-keys/rotate/begin   — MK ローテーション開始
- POST   /api/v1/wrapped-keys/rotate/commit  — 完了 (旧 wrapped_keys 削除)
- POST   /api/v1/wrapped-keys/rotate/abort   — 中止 (新 wrapped_keys 削除)
"""

import hashlib
import secrets
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request
from sqlalchemy.exc import IntegrityError

from app.extensions import db, limiter
from app.models.user import User
from app.models.webauthn import WebAuthnCredential
from app.models.wrapped_key import (
    ALLOWED_METHODS,
    METHOD_PASSKEY_PRF,
    METHOD_PASSPHRASE,
    METHOD_RECOVERY_SEED,
    WrappedKey,
)
from app.services.api_auth import auth_required, rate_limit_key


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

# MK ローテーション (§10.5)
ROTATION_TTL = timedelta(days=7)
ROTATION_TOKEN_PREFIX = "rot_"


def _hash_rotation_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _verify_rotation_token(user: User) -> bool:
    """X-Rotation-Id ヘッダの token を user.mk_rotation_state と照合。"""
    raw = request.headers.get("X-Rotation-Id", "")
    if not raw:
        return False
    state = user.mk_rotation_state or {}
    stored_hash = state.get("rotation_token_hash")
    if not stored_hash:
        return False
    return secrets.compare_digest(
        stored_hash, _hash_rotation_token(raw)
    )


def _record_new_wrapped_key_id(user: User, wrapped_key_id: int) -> None:
    """ローテーション中の新 wrapped_keys に id を記録 (state.new_wrapped_keys_id_set)。

    SQLAlchemy の JSON column は in-place mutation を検知しないため、
    必ず新しい dict を作って user.mk_rotation_state に代入する。
    """
    state = dict(user.mk_rotation_state or {})
    ids = list(state.get("new_wrapped_keys_id_set", []))
    if wrapped_key_id not in ids:
        ids.append(wrapped_key_id)
    state["new_wrapped_keys_id_set"] = ids
    user.mk_rotation_state = state


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
@limiter.limit("120 per hour", key_func=rate_limit_key)
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
@limiter.limit("20 per hour", key_func=rate_limit_key)
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
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        # UNIQUE 制約違反 (passphrase / recovery_seed の重複 or 同 credential 重複)
        return jsonify(error="conflict with existing wrapped_key"), 409

    # X-Rotation-Id ヘッダがあれば new_wrapped_keys_id_set に登録 (§10.5)。
    # 並行性注意: 別リクエストの abort が flush〜commit の間に走ると、
    # この commit が state を旧 dict で上書きして「state 復活」する可能性が
    # ある。ローテーション中の並行操作は実用上避けられる (UI で 1 つの
    # rotation 操作中は他をブロック) ので許容範囲。
    if _verify_rotation_token(g.auth_user):
        _record_new_wrapped_key_id(g.auth_user, row.id)

    db.session.commit()
    return jsonify(_serialize(row)), 201


@bp.put("/<int:wrapped_key_id>/touch")
@auth_required(write=True)
# per-user で 60 req/hour、per-IP で 5000 req/hour (設計書 §10.9)
@limiter.limit("60 per hour", key_func=rate_limit_key)
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
@limiter.limit("20 per hour", key_func=rate_limit_key)
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

    # TOCTOU 対策: PostgreSQL では同一ユーザーの wrapped_keys 全行に
    # SELECT ... FOR UPDATE を取り、残件数チェック → DELETE をトランザクション
    # 内で実行。並行 DELETE はロック待ちでシリアライズされる。
    # SQLite では行ロックは no-op だが、flush→count→rollback で論理的に
    # 等価な動作になる (一方が勝って rollback、もう一方は最終チェックで残件数 1)。
    db.session.execute(
        db.select(WrappedKey)
        .where(WrappedKey.user_id == g.auth_user.id)
        .with_for_update()
    )
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


# --- MK ローテーション (設計書 §10.5) -------------------------------------


@bp.post("/rotate/begin")
@auth_required(write=True)
@limiter.limit("5 per hour", key_func=rate_limit_key)
def rotate_begin():
    """ローテーション開始: rotation_token を発行し state を初期化。

    レスポンスで返す raw token は **1 回限り**。クライアントは X-Rotation-Id
    ヘッダで以後の write 操作を識別する。
    """
    user = g.auth_user
    state = user.mk_rotation_state
    if state and state.get("status") == "rotating":
        return jsonify(error="rotation already in progress"), 409

    now = datetime.now(timezone.utc)
    raw_token = ROTATION_TOKEN_PREFIX + secrets.token_hex(32)
    user.mk_rotation_state = {
        "status": "rotating",
        "started_at": now.isoformat(),
        "rotation_token_hash": _hash_rotation_token(raw_token),
        "auto_abort_at": (now + ROTATION_TTL).isoformat(),
        "new_wrapped_keys_id_set": [],
    }
    db.session.commit()
    return jsonify({
        "rotation_token": raw_token,
        "auto_abort_at": user.mk_rotation_state["auto_abort_at"],
    }), 201


@bp.post("/rotate/commit")
@auth_required(write=True)
@limiter.limit("10 per hour", key_func=rate_limit_key)
def rotate_commit():
    """ローテーション完了: state.new_wrapped_keys_id_set に含まれない旧 wrapped_keys を削除。

    X-Rotation-Id ヘッダ必須。state は NULL クリア。
    """
    user = g.auth_user
    if not _verify_rotation_token(user):
        return jsonify(error="invalid rotation token"), 403

    state = user.mk_rotation_state or {}
    if state.get("status") != "rotating":
        return jsonify(error="no rotation in progress"), 409

    new_set = set(state.get("new_wrapped_keys_id_set", []))
    if not new_set:
        # commit する new wrapped_key が無い場合は abort で state クリアして
        # やり直すのが正しいフロー (commit は new_set を残さないため)
        return jsonify(
            error="no new wrapped_keys recorded; use /rotate/abort to clear state"
        ), 409

    # user_id フィルタを厳格適用して旧 wrapped_keys を削除 (IDOR 防止)。
    # new_set に含まれない自身の行のみが削除対象。
    deleted = (
        WrappedKey.query
        .filter_by(user_id=user.id)
        .filter(~WrappedKey.id.in_(new_set))
        .delete(synchronize_session=False)
    )
    user.mk_rotation_state = None
    db.session.commit()
    return jsonify(deleted=deleted), 200


@bp.post("/rotate/abort")
@auth_required(write=True)
@limiter.limit("10 per hour", key_func=rate_limit_key)
def rotate_abort():
    """ローテーション中止: state.new_wrapped_keys_id_set の行を削除。

    X-Rotation-Id ヘッダ必須。state は NULL クリア。データ本体は再暗号化が
    未完了なので旧 MK で復号可能なまま (設計書 §10.5)。
    """
    user = g.auth_user
    if not _verify_rotation_token(user):
        return jsonify(error="invalid rotation token"), 403

    state = user.mk_rotation_state or {}
    if state.get("status") != "rotating":
        return jsonify(error="no rotation in progress"), 409

    new_set = state.get("new_wrapped_keys_id_set", [])
    deleted = 0
    if new_set:
        deleted = (
            WrappedKey.query
            .filter_by(user_id=user.id)
            .filter(WrappedKey.id.in_(new_set))
            .delete(synchronize_session=False)
        )
    user.mk_rotation_state = None
    db.session.commit()
    return jsonify(deleted=deleted), 200
