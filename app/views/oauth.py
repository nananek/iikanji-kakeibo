"""OAuth 2.0 Device Authorization Grant (RFC 8628)"""

from datetime import datetime, timezone

from flask import Blueprint, jsonify, redirect, render_template, request, url_for, flash
from flask_login import current_user, login_required

from app.extensions import db, limiter
from app.models.oauth import (
    OAuthDevice, OAuthToken,
    DEVICE_CODE_EXPIRES_IN, DEVICE_CODE_POLL_INTERVAL,
)

bp = Blueprint("oauth", __name__, url_prefix="/oauth")

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _expire_pending(device: OAuthDevice) -> bool:
    """期限切れの pending を expired に遷移させる。遷移したら True。"""
    if device.status == "pending" and device.is_expired():
        device.status = "expired"
        db.session.commit()
        return True
    return False


@bp.route("/device", methods=["POST"])
@limiter.limit("30/hour")
def device_authorization():
    """RFC 8628 §3.1 - Device Authorization Request

    認証不要。クライアントが device_code / user_code を取得する。
    """
    client_name = None
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        client_name = payload.get("client_name")
    if not client_name:
        client_name = request.form.get("client_name")

    raw_device_code, device = OAuthDevice.create_pending(client_name=client_name)
    db.session.add(device)
    db.session.commit()

    verification_uri = url_for("oauth.device_verification", _external=True)
    verification_uri_complete = (
        f"{verification_uri}?code={device.user_code}"
    )
    return jsonify({
        "device_code": raw_device_code,
        "user_code": device.user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": verification_uri_complete,
        "expires_in": DEVICE_CODE_EXPIRES_IN,
        "interval": DEVICE_CODE_POLL_INTERVAL,
    })


@bp.route("/token", methods=["POST"])
@limiter.limit("120/hour")
def token():
    """RFC 8628 §3.4 - Device Access Token Request

    クライアントがポーリングして承認状態を確認しトークンを取得する。
    """
    payload = request.get_json(silent=True) or request.form

    grant_type = payload.get("grant_type")
    if grant_type != DEVICE_GRANT_TYPE:
        return jsonify({"error": "unsupported_grant_type"}), 400

    raw_device_code = payload.get("device_code")
    if not raw_device_code:
        return jsonify({"error": "invalid_request"}), 400

    device_hash = OAuthDevice.hash_device_code(raw_device_code)
    device = OAuthDevice.query.filter_by(device_code_hash=device_hash).first()
    if not device:
        return jsonify({"error": "invalid_grant"}), 400

    # ポーリング間隔チェック (slow_down)
    now = _now_utc()
    if device.last_polled_at is not None:
        last = device.last_polled_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = (now - last).total_seconds()
        if elapsed < DEVICE_CODE_POLL_INTERVAL:
            device.last_polled_at = now
            db.session.commit()
            return jsonify({"error": "slow_down"}), 400
    device.last_polled_at = now

    _expire_pending(device)

    if device.status == "expired":
        db.session.commit()
        return jsonify({"error": "expired_token"}), 400
    if device.status == "denied":
        db.session.commit()
        return jsonify({"error": "access_denied"}), 400
    if device.status == "consumed":
        db.session.commit()
        return jsonify({"error": "invalid_grant"}), 400
    if device.status == "pending":
        db.session.commit()
        return jsonify({"error": "authorization_pending"}), 400

    # approved
    if device.status != "approved" or device.user_id is None:
        db.session.commit()
        return jsonify({"error": "invalid_grant"}), 400

    raw_token, token_hash, prefix = OAuthToken.generate()
    name = device.client_name or "OAuth Device"
    oauth_token = OAuthToken(
        user_id=device.user_id,
        name=name[:100],
        token_hash=token_hash,
        token_prefix=prefix,
        read_only=device.read_only,
    )
    device.status = "consumed"
    db.session.add(oauth_token)
    db.session.commit()

    return jsonify({
        "access_token": raw_token,
        "token_type": "Bearer",
        # 1年間有効（実際の失効はサーバー側のレコード状態のみ）
        "expires_in": 31536000,
    })


@bp.route("/device", methods=["GET"])
@login_required
def device_verification():
    """ユーザーが user_code を入力して承認/拒否する画面"""
    code = (request.args.get("code") or "").strip().upper()
    device = None
    expired = False
    if code:
        normalized = _normalize_user_code(code)
        device = OAuthDevice.query.filter_by(user_code=normalized).first()
        if device:
            expired = _expire_pending(device)
    return render_template(
        "oauth/device_verification.html",
        code=code,
        device=device,
        expired=expired,
    )


def _normalize_user_code(value: str) -> str:
    """user_code を XXXX-XXXX 形式に正規化する"""
    upper = value.replace(" ", "").replace("-", "").upper()
    if len(upper) == 8:
        return f"{upper[:4]}-{upper[4:]}"
    return value.upper()


@bp.route("/device/authorize", methods=["POST"])
@login_required
def device_authorize():
    """user_code に対してユーザーが承認/拒否する処理"""
    raw = request.form.get("user_code", "")
    decision = request.form.get("decision", "approve")
    user_code = _normalize_user_code(raw.strip())

    if not user_code:
        flash("コードを入力してください。", "warning")
        return redirect(url_for("oauth.device_verification"))

    device = OAuthDevice.query.filter_by(user_code=user_code).first()
    if not device:
        flash("コードが見つかりません。", "danger")
        return redirect(url_for("oauth.device_verification", code=raw))

    _expire_pending(device)

    if device.status != "pending":
        flash("このコードは既に処理済みです。", "warning")
        return redirect(url_for("oauth.device_verification", code=raw))

    if decision == "deny":
        device.status = "denied"
        db.session.commit()
        flash("接続を拒否しました。", "info")
        return redirect(url_for("oauth.device_verification", code=raw))

    device.status = "approved"
    device.user_id = current_user.id
    device.read_only = (decision == "approve_readonly")
    db.session.commit()
    if device.read_only:
        flash("読み取り専用で接続を承認しました。クライアントに戻ってください。", "success")
    else:
        flash("接続を承認しました。クライアントに戻ってください。", "success")
    return redirect(url_for("oauth.device_verification", code=raw))
