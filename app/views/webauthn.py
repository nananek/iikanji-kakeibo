import logging
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
from app.views.helpers import maybe_clear_pending_recovery

logger = logging.getLogger(__name__)

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
    except Exception:
        logger.warning("WebAuthn registration verification failed", exc_info=True)
        return jsonify(error="検証に失敗しました。もう一度お試しください。"), 400

    transports = " / ".join(body.get("response", {}).get("transports", []))

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

    # Phase 6 #71: 送信失敗は本体フロー (登録成功) に波及させない
    if current_user.email:
        from app.services.mail import send_email
        send_email(
            current_user.email,
            "security_alert",
            {
                "username": current_user.username,
                "event_type": "passkey_added",
                "event_label": "新しいパスキーが追加されました",
                "passkey_name": credential.name or "(無名)",
                "transports": transports or "不明",
                "event_at": datetime.now(timezone.utc)
                    .strftime("%Y-%m-%d %H:%M:%S UTC"),
                "client_ip": request.remote_addr or "不明",
                "user_agent": request.headers.get("User-Agent", "") or "不明",
            },
        )

    # リカバリログイン後の強制復旧フロー: パスキーが新規登録され
    # かつ新リカバリコードも生成済みなら、pending 状態を解除する
    maybe_clear_pending_recovery(current_user, session)

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
    except Exception:
        logger.warning("WebAuthn authentication verification failed", exc_info=True)
        return jsonify(error="認証に失敗しました。もう一度お試しください。"), 400

    # sign_count 更新
    stored.current_sign_count = verification.new_sign_count
    stored.last_used_at = datetime.now(timezone.utc)
    db.session.commit()

    login_user(stored.user, remember=True)
    next_page = request.args.get("next", "/")
    parsed = urlparse(next_page)
    if parsed.netloc or parsed.scheme:
        next_page = "/"
    return jsonify(ok=True, redirect=next_page)


# ---------- 鍵派生用 認証 (login しない、E1 PR-F3a) ----------
#
# 通常の認証 (`/authenticate/verify`) は login_user を呼んでセッションを開く
# が、鍵派生用途では「ログイン済みユーザーが Passkey をタッチして PRF
# 出力を取り出す」ことだけが目的。session login はせず、credential の
# 所有者検証と sign_count 更新のみ行う。
#
# PRF 出力 (32B) は authenticator がクライアントに返し、サーバには到達
# しない。サーバが返すのは「この credential はあなたの所有である」確認
# と DB PK (`webauthn_credentials.id`)。クライアントはこの DB PK を
# wrapped_keys.webauthn_credential_id に紐付けて保存する。


@bp.route("/key-derivation/options", methods=["POST"])
@login_required
def key_derivation_options():
    """鍵派生用 WebAuthn 認証チャレンジを生成。

    Body (JSON): なし or {"credential_id": <db PK>} — 特定 Passkey に限定する場合
    Returns: PublicKeyCredentialRequestOptions JSON。allow_credentials は
             current_user の Passkey に限定する (他ユーザーの Passkey は使えない)。
             PRF 拡張入力はクライアント側で挿入する (webauthn_prf.js
             getPrfEvalInputBytes() を使用)。
    """
    body = request.get_json(silent=True) or {}
    target_db_id = body.get("credential_id")

    # credential_id は省略可だが、指定する場合は正の整数のみ受理。
    # 文字列・配列を渡すと SQLAlchemy が PostgreSQL に INTEGER = 'abc' を
    # 送って InvalidTextRepresentation → 500 エラーになる経路を塞ぐ。
    if target_db_id is not None:
        # bool は int のサブクラスなので明示的に除外
        if isinstance(target_db_id, bool) or not isinstance(target_db_id, int):
            return jsonify(
                error="credential_id は正の整数で指定してください。",
            ), 400
        if target_db_id <= 0:
            return jsonify(
                error="credential_id は正の整数で指定してください。",
            ), 400

    query = WebAuthnCredential.query.filter_by(user_id=current_user.id)
    if target_db_id is not None:
        query = query.filter(WebAuthnCredential.id == target_db_id)
    credentials = query.all()
    if not credentials:
        return jsonify(error="Passkey が登録されていません。"), 400

    rp_id, _ = _get_rp_id_and_origin()
    options = generate_authentication_options(
        rp_id=rp_id,
        user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=c.credential_id)
            for c in credentials
        ],
    )
    session["webauthn_key_derivation_challenge"] = options.challenge
    return current_app.response_class(
        options_to_json(options), content_type="application/json"
    )


@bp.route("/key-derivation/finalize", methods=["POST"])
@login_required
def key_derivation_finalize():
    """鍵派生フローの所有権検証 + sign_count 更新。

    Body (JSON): WebAuthn credential response (rawId, response, etc.)
    Returns: {"ok": true, "credential_id": <db PK>}

    通常の認証と異なり session login は行わない。current_user は既に
    Flask-Login でログイン済みである前提 (`@login_required`)。本人の
    credential であることを確認することで「他のタブで他人の Passkey
    使われる」リスクを排除する。
    """
    challenge = session.pop("webauthn_key_derivation_challenge", None)
    if not challenge:
        return jsonify(error="チャレンジが見つかりません。"), 400

    body = request.get_json()
    raw_id = body.get("rawId", "")
    import base64

    try:
        credential_id_bytes = base64.urlsafe_b64decode(raw_id + "==")
    except Exception:
        return jsonify(error="無効なクレデンシャル ID です。"), 400

    stored = WebAuthnCredential.query.filter_by(
        credential_id=credential_id_bytes,
        user_id=current_user.id,  # 本人の credential のみ受理 (IDOR 対策)
    ).first()
    if not stored:
        return jsonify(error="このユーザーに紐付かない Passkey です。"), 400

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
    except Exception:
        logger.warning(
            "WebAuthn key-derivation verification failed", exc_info=True
        )
        return jsonify(error="認証に失敗しました。"), 400

    stored.current_sign_count = verification.new_sign_count
    stored.last_used_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify(ok=True, credential_id=stored.id)
