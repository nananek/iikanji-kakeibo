"""E2EE 監査連携 API — audit_packages / audit_responses (E5 #112 PR-C / §14.5)。

HPKE 非同期ワークフロー監査のサーバ側エンドポイント。**暗号化はクライアント責務**で、
サーバは HPKE 暗号文 (ephemeral_pubkey + ciphertext) を預かり、IDOR / 失効 / round を
検証するだけ。平文スナップショットには一切触らない。

エンドポイント:
- GET    /api/v1/audit-packages              — 自分が owner または auditor のもの
- POST   /api/v1/audit-packages              — owner が新規スナップショットを積む
- POST   /api/v1/audit-packages/<id>/accept  — owner が採用確定 (owner_accepted_at)
- DELETE /api/v1/audit-packages/<id>         — owner / auditor が削除 (CASCADE)
- GET    /api/v1/audit-responses             — 自分が関係する response
- POST   /api/v1/audit-responses             — auditor が修正案 / 差戻しを返す
- POST   /api/v1/audit-responses/<id>/acknowledge — owner が確認 (owner_acknowledged_at)
"""

from base64 import b64decode, b64encode
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request
from sqlalchemy.exc import IntegrityError

from app.extensions import db, limiter
from app.models.audit import (
    ALLOWED_RESPONSE_TYPES,
    AuditGrant,
    AuditPackage,
    AuditResponse,
)
from app.services.api_auth import auth_required, rate_limit_key, reject_if_proxy


bp = Blueprint("audit_packages", __name__, url_prefix="/api/v1")

# X25519 ephemeral 公開鍵 / SHA-256 ハッシュは 32B 固定。
PUBKEY_LEN = 32
HASH_LEN = 32
# HPKE 暗号文 (スナップショット JSON) の上限。証憑画像同梱は後続スコープだが、
# 仕訳/残高スナップショットでもある程度の大きさになるため余裕を持たせる。
MAX_CIPHERTEXT_SIZE = 16 * 1024 * 1024  # 16 MiB


def _b64(b: bytes | None) -> str | None:
    return b64encode(b).decode("ascii") if b is not None else None


def _b64_or_none(payload: dict, key: str) -> tuple[bytes | None, str | None]:
    """payload[key] を base64 デコード。例外詳細は捨て固定文字列のみ返す
    (CodeQL stack-trace-exposure 回避、wrapped_keys.py と同方針)。"""
    val = payload.get(key)
    if val is None:
        return None, f"{key} is required"
    if not isinstance(val, str):
        return None, f"{key} must be a base64 string"
    try:
        return b64decode(val, validate=True), None
    except Exception:
        return None, f"{key} is not valid base64"


def _now():
    return datetime.now(timezone.utc)


def _is_expired(dt) -> bool:
    """expires_at が現在より過去か。SQLite は tz を落として naive で返すため、
    naive な値は UTC とみなして aware な now と比較する (PostgreSQL は aware)。"""
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < _now()


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


# --- シリアライズ --------------------------------------------------------


def _serialize_package(p: AuditPackage) -> dict:
    return {
        "id": p.id,
        "audit_grant_id": p.audit_grant_id,
        "round_id": p.round_id,
        "owner_user_id": p.owner_user_id,
        "auditor_user_id": p.auditor_user_id,
        "permission_level": p.permission_level,
        "ephemeral_pubkey": _b64(p.ephemeral_pubkey),
        "ciphertext": _b64(p.ciphertext),
        "snapshot_hash": _b64(p.snapshot_hash),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
        "owner_accepted_at": (
            p.owner_accepted_at.isoformat() if p.owner_accepted_at else None
        ),
    }


def _serialize_response(r: AuditResponse) -> dict:
    return {
        "id": r.id,
        "audit_package_id": r.audit_package_id,
        "response_type": r.response_type,
        "ephemeral_pubkey": _b64(r.ephemeral_pubkey),
        "ciphertext": _b64(r.ciphertext),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "owner_acknowledged_at": (
            r.owner_acknowledged_at.isoformat()
            if r.owner_acknowledged_at else None
        ),
    }


# --- audit_packages ------------------------------------------------------


@bp.get("/audit-packages")
@auth_required(write=False)
@limiter.limit("120 per hour", key_func=rate_limit_key)
def list_audit_packages():
    """自分が owner または auditor の AuditPackage 一覧。?role=owner|auditor で絞込。"""
    proxy_err = reject_if_proxy()
    if proxy_err is not None:
        return proxy_err
    me = g.auth_user.id
    role = request.args.get("role")
    q = AuditPackage.query
    if role == "owner":
        q = q.filter(AuditPackage.owner_user_id == me)
    elif role == "auditor":
        q = q.filter(AuditPackage.auditor_user_id == me)
    else:
        q = q.filter(
            db.or_(
                AuditPackage.owner_user_id == me,
                AuditPackage.auditor_user_id == me,
            )
        )
    rows = q.order_by(AuditPackage.id.asc()).all()
    return jsonify(audit_packages=[_serialize_package(p) for p in rows])


@bp.post("/audit-packages")
@auth_required(write=True)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def create_audit_package():
    """owner が新しい監査スナップショットを積む。

    owner / auditor は grant から **サーバ側で導出** する (クライアント値を信用しない)。
    失効 grant は 403、(grant, round) 重複は 409。
    """
    proxy_err = reject_if_proxy()
    if proxy_err is not None:
        return proxy_err
    me = g.auth_user.id
    payload = request.get_json(silent=True) or {}

    grant_id = payload.get("audit_grant_id")
    round_id = payload.get("round_id")
    permission_level = payload.get("permission_level")
    if not _is_int(grant_id) or not _is_int(round_id):
        return jsonify(error="audit_grant_id and round_id must be int"), 400
    if not _is_int(permission_level):
        return jsonify(error="permission_level must be int"), 400
    if round_id < 1:
        return jsonify(error="round_id must be >= 1"), 400

    grant = db.session.get(AuditGrant, grant_id)
    if grant is None:
        return jsonify(error="audit_grant not found"), 404
    # owner のみが作成可。他人の grant への IDOR は 403。
    if grant.owner_user_id != me:
        return jsonify(error="forbidden"), 403
    if grant.revoked_at is not None:
        return jsonify(error="audit grant has been revoked"), 403
    # snapshot は grant の権限レベルで作られているはず。不一致はクライアントバグ。
    if permission_level != grant.permission_level:
        return jsonify(error="permission_level does not match grant"), 400

    ephemeral_pubkey, err = _b64_or_none(payload, "ephemeral_pubkey")
    if err is not None:
        return jsonify(error=err), 400
    ciphertext, err = _b64_or_none(payload, "ciphertext")
    if err is not None:
        return jsonify(error=err), 400
    snapshot_hash, err = _b64_or_none(payload, "snapshot_hash")
    if err is not None:
        return jsonify(error=err), 400

    if len(ephemeral_pubkey) != PUBKEY_LEN:
        return jsonify(error=f"ephemeral_pubkey must be {PUBKEY_LEN} bytes"), 400
    if len(snapshot_hash) != HASH_LEN:
        return jsonify(error=f"snapshot_hash must be {HASH_LEN} bytes"), 400
    if not ciphertext:
        return jsonify(error="ciphertext is required"), 400
    if len(ciphertext) > MAX_CIPHERTEXT_SIZE:
        return jsonify(error="ciphertext too large"), 400

    pkg = AuditPackage(
        audit_grant_id=grant.id,
        round_id=round_id,
        owner_user_id=grant.owner_user_id,
        auditor_user_id=grant.auditor_user_id,
        permission_level=grant.permission_level,
        ephemeral_pubkey=ephemeral_pubkey,
        ciphertext=ciphertext,
        snapshot_hash=snapshot_hash,
    )
    db.session.add(pkg)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error="package for this (grant, round) already exists"), 409
    db.session.commit()
    return jsonify(_serialize_package(pkg)), 201


@bp.post("/audit-packages/<int:package_id>/accept")
@auth_required(write=True)
@limiter.limit("120 per hour", key_func=rate_limit_key)
def accept_audit_package(package_id: int):
    """owner が修正案を採用確定する (owner_accepted_at をセット, §14.2)。"""
    proxy_err = reject_if_proxy()
    if proxy_err is not None:
        return proxy_err
    pkg = db.session.get(AuditPackage, package_id)
    if pkg is None:
        return jsonify(error="not found"), 404
    if pkg.owner_user_id != g.auth_user.id:
        return jsonify(error="forbidden"), 403
    pkg.owner_accepted_at = _now()
    db.session.commit()
    return jsonify(_serialize_package(pkg))


@bp.delete("/audit-packages/<int:package_id>")
@auth_required(write=True)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def delete_audit_package(package_id: int):
    """owner / auditor が AuditPackage を削除 (responses も CASCADE)。"""
    proxy_err = reject_if_proxy()
    if proxy_err is not None:
        return proxy_err
    me = g.auth_user.id
    pkg = db.session.get(AuditPackage, package_id)
    if pkg is None:
        return jsonify(error="not found"), 404
    if me not in (pkg.owner_user_id, pkg.auditor_user_id):
        return jsonify(error="forbidden"), 403
    db.session.delete(pkg)
    db.session.commit()
    return "", 204


# --- audit_responses -----------------------------------------------------


@bp.get("/audit-responses")
@auth_required(write=False)
@limiter.limit("120 per hour", key_func=rate_limit_key)
def list_audit_responses():
    """自分が関係する (package 経由で owner または auditor) response 一覧。"""
    proxy_err = reject_if_proxy()
    if proxy_err is not None:
        return proxy_err
    me = g.auth_user.id
    rows = (
        AuditResponse.query
        .join(AuditPackage, AuditResponse.audit_package_id == AuditPackage.id)
        .filter(
            db.or_(
                AuditPackage.owner_user_id == me,
                AuditPackage.auditor_user_id == me,
            )
        )
        .order_by(AuditResponse.id.asc())
        .all()
    )
    return jsonify(audit_responses=[_serialize_response(r) for r in rows])


@bp.post("/audit-responses")
@auth_required(write=True)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def create_audit_response():
    """auditor が修正案 / 差戻しを返す (HPKE owner 公開鍵宛, §14.2)。"""
    proxy_err = reject_if_proxy()
    if proxy_err is not None:
        return proxy_err
    me = g.auth_user.id
    payload = request.get_json(silent=True) or {}

    package_id = payload.get("audit_package_id")
    response_type = payload.get("response_type")
    if not _is_int(package_id):
        return jsonify(error="audit_package_id must be int"), 400
    if response_type not in ALLOWED_RESPONSE_TYPES:
        return jsonify(
            error=f"response_type must be one of {list(ALLOWED_RESPONSE_TYPES)}"
        ), 400

    pkg = db.session.get(AuditPackage, package_id)
    if pkg is None:
        return jsonify(error="audit_package not found"), 404
    # auditor のみが response を返せる。
    if pkg.auditor_user_id != me:
        return jsonify(error="forbidden"), 403
    # 失効 grant / 期限切れ package には返答不可。
    grant = db.session.get(AuditGrant, pkg.audit_grant_id)
    if grant is not None and grant.revoked_at is not None:
        return jsonify(error="audit grant has been revoked"), 403
    if _is_expired(pkg.expires_at):
        return jsonify(error="audit package has expired"), 403

    ephemeral_pubkey, err = _b64_or_none(payload, "ephemeral_pubkey")
    if err is not None:
        return jsonify(error=err), 400
    ciphertext, err = _b64_or_none(payload, "ciphertext")
    if err is not None:
        return jsonify(error=err), 400
    if len(ephemeral_pubkey) != PUBKEY_LEN:
        return jsonify(error=f"ephemeral_pubkey must be {PUBKEY_LEN} bytes"), 400
    if not ciphertext:
        return jsonify(error="ciphertext is required"), 400
    if len(ciphertext) > MAX_CIPHERTEXT_SIZE:
        return jsonify(error="ciphertext too large"), 400

    resp = AuditResponse(
        audit_package_id=pkg.id,
        response_type=response_type,
        ephemeral_pubkey=ephemeral_pubkey,
        ciphertext=ciphertext,
    )
    db.session.add(resp)
    db.session.commit()
    return jsonify(_serialize_response(resp)), 201


@bp.post("/audit-responses/<int:response_id>/acknowledge")
@auth_required(write=True)
@limiter.limit("120 per hour", key_func=rate_limit_key)
def acknowledge_audit_response(response_id: int):
    """owner が修正案 / 差戻しを確認済みにする (owner_acknowledged_at)。"""
    proxy_err = reject_if_proxy()
    if proxy_err is not None:
        return proxy_err
    me = g.auth_user.id
    resp = db.session.get(AuditResponse, response_id)
    if resp is None:
        return jsonify(error="not found"), 404
    pkg = db.session.get(AuditPackage, resp.audit_package_id)
    if pkg is None or pkg.owner_user_id != me:
        return jsonify(error="forbidden"), 403
    resp.owner_acknowledged_at = _now()
    db.session.commit()
    return jsonify(_serialize_response(resp))
