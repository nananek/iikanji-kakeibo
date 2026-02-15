from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Blueprint, current_app, jsonify, request, session
from flask_login import current_user, login_required, login_user

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.extensions import db
from app.models.user import User
from app.models.webauthn import WebAuthnCredential

bp = Blueprint("webauthn", __name__, url_prefix="/webauthn")


def _get_rp_id_and_origin():
    """リクエストの Origin ヘッダーから RP_ID と origin を決定する。
    localhost / 127.0.0.1 どちらでアクセスしても動作するようにする。"""
    origin = request.headers.get("Origin") or request.host_url.rstrip("/")
    parsed = urlparse(origin)
    rp_id = parsed.hostname  # e.g. "localhost" or "127.0.0.1"
    return rp_id, origin


# ---------- 登録 ----------


@bp.route("/register/options", methods=["POST"])
@login_required
def register_options():
    """登録セレモニー: PublicKeyCredentialCreationOptions を生成"""
    existing = WebAuthnCredential.query.filter_by(user_id=current_user.id).all()
    exclude_credentials = [
        PublicKeyCredentialDescriptor(id=c.credential_id) for c in existing
    ]

    rp_id, _ = _get_rp_id_and_origin()
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=current_app.config["WEBAUTHN_RP_NAME"],
        user_id=str(current_user.id).encode(),
        user_name=current_user.username,
        user_display_name=current_user.username,
        exclude_credentials=exclude_credentials,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )

    session["webauthn_register_challenge"] = options.challenge
    return current_app.response_class(
        options_to_json(options), content_type="application/json"
    )


@bp.route("/register/verify", methods=["POST"])
@login_required
def register_verify():
    """登録セレモニー: ブラウザからのレスポンスを検証し保存"""
    challenge = session.pop("webauthn_register_challenge", None)
    if not challenge:
        return jsonify(error="チャレンジが見つかりません。もう一度お試しください。"), 400

    body = request.get_json()
    rp_id, origin = _get_rp_id_and_origin()
    try:
        verification = verify_registration_response(
            credential=body,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
        )
    except Exception as e:
        return jsonify(error=f"検証に失敗しました: {e}"), 400

    transports = ",".join(body.get("response", {}).get("transports", []))

    credential = WebAuthnCredential(
        user_id=current_user.id,
        credential_id=verification.credential_id,
        credential_public_key=verification.credential_public_key,
        current_sign_count=verification.sign_count,
        name=body.get("passkey_name", ""),
        transports=transports or None,
    )
    db.session.add(credential)
    db.session.commit()

    return jsonify(ok=True, id=credential.id, name=credential.name)


# ---------- 認証 ----------


@bp.route("/authenticate/options", methods=["POST"])
def authenticate_options():
    """認証セレモニー: PublicKeyCredentialRequestOptions を生成"""
    rp_id, _ = _get_rp_id_and_origin()
    options = generate_authentication_options(
        rp_id=rp_id,
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    session["webauthn_auth_challenge"] = options.challenge
    return current_app.response_class(
        options_to_json(options), content_type="application/json"
    )


@bp.route("/authenticate/verify", methods=["POST"])
def authenticate_verify():
    """認証セレモニー: ブラウザからのレスポンスを検証しログイン"""
    challenge = session.pop("webauthn_auth_challenge", None)
    if not challenge:
        return jsonify(error="チャレンジが見つかりません。もう一度お試しください。"), 400

    body = request.get_json()
    raw_id = body.get("rawId", "")

    # credential_id をバイナリに変換して検索
    import base64

    try:
        credential_id_bytes = base64.urlsafe_b64decode(raw_id + "==")
    except Exception:
        return jsonify(error="無効なクレデンシャルIDです。"), 400

    stored = WebAuthnCredential.query.filter_by(
        credential_id=credential_id_bytes
    ).first()
    if not stored:
        return jsonify(error="登録されていないPasskeyです。"), 400

    rp_id, origin = _get_rp_id_and_origin()
    try:
        verification = verify_authentication_response(
            credential=body,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=stored.credential_public_key,
            credential_current_sign_count=stored.current_sign_count,
        )
    except Exception as e:
        return jsonify(error=f"認証に失敗しました: {e}"), 400

    # sign_count 更新
    stored.current_sign_count = verification.new_sign_count
    stored.last_used_at = datetime.now(timezone.utc)
    db.session.commit()

    login_user(stored.user, remember=True)
    return jsonify(ok=True, redirect=request.args.get("next", "/"))
