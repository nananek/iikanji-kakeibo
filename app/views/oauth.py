"""OAuth 2.0 Device Authorization Grant (RFC 8628) + MCP 向け OAuth 2.1

- Device Authorization Grant (RFC 8628): CLI / TUI などのブラウザレス
- Authorization Code + PKCE (RFC 7636) + 動的クライアント登録 (RFC 7591):
  MCP クライアント (Claude カスタムコネクタ / ChatGPT 等) 向け
"""

import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app.extensions import db, limiter
from app.models.oauth import (
    OAuthDevice, OAuthToken, OAuthClient, OAuthAuthorizationCode,
    DEVICE_CODE_EXPIRES_IN, DEVICE_CODE_POLL_INTERVAL,
    AUTHORIZATION_CODE_EXPIRES_IN, ACCESS_TOKEN_EXPIRES_IN,
)
from app.services.oauth_scopes import (
    SCOPES, READ_SCOPES, normalize, read_only_for,
)

bp = Blueprint("oauth", __name__, url_prefix="/oauth")

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
AUTHORIZATION_CODE_GRANT_TYPE = "authorization_code"
REFRESH_TOKEN_GRANT_TYPE = "refresh_token"


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

    from app.services.urls import external_base_url
    verification_uri = external_base_url() + "/oauth/device"
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
    """RFC 6749 §3.2 / RFC 8628 §3.4 - Token Request

    対応 grant_type:
      - authorization_code (MCP クライアント, PKCE 必須)
      - refresh_token
      - urn:ietf:params:oauth:grant-type:device_code
    """
    payload = request.get_json(silent=True) or request.form
    grant_type = payload.get("grant_type")

    if grant_type == DEVICE_GRANT_TYPE:
        return _handle_device_grant(payload)
    if grant_type == AUTHORIZATION_CODE_GRANT_TYPE:
        return _handle_authorization_code_grant(payload)
    if grant_type == REFRESH_TOKEN_GRANT_TYPE:
        return _handle_refresh_token_grant(payload)

    return jsonify({"error": "unsupported_grant_type"}), 400


def _handle_device_grant(payload):
    """RFC 8628: クライアントがポーリングして承認状態を確認する。"""
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

    scopes = _scope_list(device.scopes)
    token_scopes = None if device.scopes is None else ",".join(scopes)
    raw_token, token_hash, prefix = OAuthToken.generate()
    name = device.client_name or "OAuth Device"
    oauth_token = OAuthToken(
        user_id=device.user_id,
        name=name[:100],
        token_hash=token_hash,
        token_prefix=prefix,
        read_only=device.read_only,
        scopes=token_scopes,
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


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """PKCE (S256): base64url(sha256(verifier)) を code_challenge と比較。"""
    if not code_verifier or not code_challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, code_challenge)


def _scope_list(value) -> list[str]:
    if not value:
        return []
    return sorted({s for s in str(value).split(",") if s})


def _authenticate_client(payload, client):
    """confidential クライアントの client_secret を検証する。"""
    if client.token_endpoint_auth_method != "client_secret_post":
        return True
    secret = payload.get("client_secret")
    if not secret or not client.client_secret_hash:
        return False
    return secrets.compare_digest(
        OAuthClient.hash_secret(secret), client.client_secret_hash
    )


def _handle_authorization_code_grant(payload):
    """RFC 6749 §4.1.3 + PKCE: 認可コードをトークンに交換する。"""
    raw_code = payload.get("code")
    client_id = payload.get("client_id")
    redirect_uri = payload.get("redirect_uri")
    code_verifier = payload.get("code_verifier")

    if not raw_code or not client_id:
        return jsonify({"error": "invalid_request"}), 400

    client = OAuthClient.query.filter_by(client_id=client_id).first()
    if not client:
        return jsonify({"error": "invalid_client"}), 401
    if not _authenticate_client(payload, client):
        return jsonify({"error": "invalid_client"}), 401
    if not code_verifier:
        return jsonify({"error": "invalid_grant"}), 400

    rec = OAuthAuthorizationCode.query.filter_by(
        code_hash=OAuthAuthorizationCode.hash_code(raw_code)
    ).first()
    if not rec or not rec.is_usable() or rec.client_id != client_id:
        return jsonify({"error": "invalid_grant"}), 400

    # redirect_uri は認可リクエスト時の値と一致必須 (§4.1.3)
    if redirect_uri and redirect_uri != rec.redirect_uri:
        return jsonify({"error": "invalid_grant"}), 400
    if not _verify_pkce(code_verifier, rec.code_challenge):
        return jsonify({"error": "invalid_grant"}), 400

    rec.used_at = _now_utc()
    scopes = _scope_list(rec.scopes)
    raw, raw_refresh, oauth_token = OAuthToken.issue(
        user_id=rec.user_id,
        name=client.client_name or "MCP クライアント",
        scopes=scopes,
        read_only=read_only_for(scopes),
        client_id=client.client_id,
    )
    client.last_used_at = _now_utc()
    db.session.commit()

    return jsonify({
        "access_token": raw,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_EXPIRES_IN,
        "refresh_token": raw_refresh,
        "scope": " ".join(scopes),
    })


def _handle_refresh_token_grant(payload):
    """RFC 6749 §6: リフレッシュトークンでアクセストークンを更新する。"""
    raw_refresh = payload.get("refresh_token")
    client_id = payload.get("client_id")
    if not raw_refresh or not client_id:
        return jsonify({"error": "invalid_request"}), 400

    client = OAuthClient.query.filter_by(client_id=client_id).first()
    if not client:
        return jsonify({"error": "invalid_client"}), 401
    if not _authenticate_client(payload, client):
        return jsonify({"error": "invalid_client"}), 401

    token = OAuthToken.query.filter_by(
        refresh_token_hash=OAuthToken.hash_token(raw_refresh)
    ).first()
    if (
        not token
        or not token.refresh_is_valid()
        or token.client_id != client_id
    ):
        return jsonify({"error": "invalid_grant"}), 400

    # アクセストークンをローテート (リフレッシュトークンも更新)
    raw_access, token_hash, prefix = OAuthToken.generate()
    token.token_hash = token_hash
    token.token_prefix = prefix
    token.expires_at = _now_utc() + timedelta(seconds=ACCESS_TOKEN_EXPIRES_IN)
    token.last_used_at = _now_utc()
    new_refresh = token.rotate_refresh()
    db.session.commit()

    return jsonify({
        "access_token": raw_access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_EXPIRES_IN,
        "refresh_token": new_refresh,
        "scope": " ".join(_scope_list(token.scopes)),
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
        all_scopes=list(SCOPES.keys()),
        read_scopes=set(READ_SCOPES),
        scope_labels=SCOPES,
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

    selected = [s for s in request.form.getlist("scope") if s in SCOPES]
    if decision == "approve_readonly":
        # 読み取り専用: 全スコープを付与しない (read_only フラグで書込拒否)
        device.read_only = True
        device.scopes = None
    elif selected:
        # ユーザーが選択したスコープのみ付与 (権限の絞り込み)
        scopes = list(dict.fromkeys(selected))
        device.scopes = ",".join(scopes)
        device.read_only = read_only_for(scopes)
    else:
        # 従来どおり全権限
        device.read_only = False
        device.scopes = None

    db.session.commit()
    if device.read_only:
        flash("読み取り専用で接続を承認しました。クライアントに戻ってください。", "success")
    else:
        flash("接続を承認しました。クライアントに戻ってください。", "success")
    return redirect(url_for("oauth.device_verification", code=raw))


# ---------------------------------------------------------------------------
# Authorization Code + PKCE (MCP クライアント向け)
# ---------------------------------------------------------------------------


def _is_valid_redirect_uri(uri) -> bool:
    if not isinstance(uri, str) or not uri or len(uri) > 1000:
        return False
    p = urlparse(uri)
    if p.fragment:
        return False
    if p.scheme == "https" and p.netloc:
        return True
    # ローカル開発クライアント向けに http://localhost を許可
    if p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    return False


def _append_query(uri: str, params: dict) -> str:
    parsed = urlparse(uri)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _parse_authorize_request(params, scope_value):
    """認可リクエストの共通検証。Returns (ctx, error_message)。"""
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    if not client_id:
        return None, "client_id が指定されていません。"
    client = OAuthClient.query.filter_by(client_id=client_id).first()
    if not client:
        return None, "不明なクライアントです。"
    if not redirect_uri or not client.allows_redirect_uri(redirect_uri):
        return None, "redirect_uri が登録されていません。"
    if params.get("response_type") != "code":
        return None, "response_type は code のみ対応しています。"
    challenge = params.get("code_challenge")
    method = params.get("code_challenge_method") or "S256"
    if not challenge or method != "S256":
        return None, "PKCE (code_challenge_method=S256) が必要です。"

    requested = normalize(scope_value)
    if not requested:
        # クライアントがスコープ未要求 → 読み取り専用を既定にする (最小権限)
        requested = list(READ_SCOPES)

    return {
        "client": client,
        "redirect_uri": redirect_uri,
        "state": params.get("state") or "",
        "code_challenge": challenge,
        "code_challenge_method": method,
        "resource": params.get("resource") or "",
        "requested_scopes": requested,
        "scope_labels": SCOPES,
    }, None


@bp.route("/authorize", methods=["GET"])
@login_required
def authorize():
    """RFC 6749 §4.1.1 - 認可エンドポイント (同意画面)"""
    ctx, error = _parse_authorize_request(request.args, request.args.get("scope"))
    if error:
        return render_template("oauth/authorize_error.html", error=error), 400
    return render_template("oauth/authorize.html", **ctx)


@bp.route("/authorize", methods=["POST"])
@login_required
def authorize_decision():
    """同意画面の送信 (承認 / 拒否)。スコープは選択した分のみ付与する。"""
    requested_value = " ".join(request.form.getlist("requested_scope"))
    ctx, error = _parse_authorize_request(request.form, requested_value)
    if error:
        return render_template("oauth/authorize_error.html", error=error), 400

    redirect_uri = ctx["redirect_uri"]
    state = ctx["state"]

    if request.form.get("decision") == "deny":
        params = {"error": "access_denied"}
        if state:
            params["state"] = state
        return redirect(_append_query(redirect_uri, params))

    selected = [
        s for s in request.form.getlist("scope")
        if s in SCOPES and s in ctx["requested_scopes"]
    ]
    selected = list(dict.fromkeys(selected))

    raw_code, code_hash = OAuthAuthorizationCode.generate()
    record = OAuthAuthorizationCode(
        code_hash=code_hash,
        client_id=ctx["client"].client_id,
        user_id=current_user.id,
        redirect_uri=redirect_uri,
        scopes=",".join(selected),
        code_challenge=ctx["code_challenge"],
        code_challenge_method="S256",
        resource=ctx["resource"] or None,
        expires_at=_now_utc() + timedelta(seconds=AUTHORIZATION_CODE_EXPIRES_IN),
    )
    db.session.add(record)
    db.session.commit()

    params = {"code": raw_code}
    if state:
        params["state"] = state
    return redirect(_append_query(redirect_uri, params))


@bp.route("/register", methods=["POST"])
@limiter.limit("30/hour")
def register():
    """RFC 7591 - 動的クライアント登録 (MCP クライアント向け, 認証不要)"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form.to_dict()

    redirect_uris = data.get("redirect_uris")
    if isinstance(redirect_uris, str):
        redirect_uris = [redirect_uris]
    if not redirect_uris or not isinstance(redirect_uris, list):
        return jsonify({
            "error": "invalid_redirect_uri",
            "error_description": "redirect_uris は必須です。",
        }), 400
    if len(redirect_uris) > 10:
        return jsonify({
            "error": "invalid_redirect_uri",
            "error_description": "redirect_uris が多すぎます。",
        }), 400
    for uri in redirect_uris:
        if not _is_valid_redirect_uri(uri):
            return jsonify({
                "error": "invalid_redirect_uri",
                "error_description": f"redirect_uri が不正です: {uri}",
            }), 400

    auth_method = data.get("token_endpoint_auth_method") or "none"
    if auth_method not in ("none", "client_secret_post"):
        auth_method = "none"

    client_name = (data.get("client_name") or "MCP クライアント")[:200]
    scope_value = " ".join(normalize(data.get("scope")))

    client = OAuthClient(
        client_id=OAuthClient.generate_client_id(),
        client_name=client_name,
        redirect_uris_json=json.dumps(redirect_uris),
        grant_types="authorization_code refresh_token",
        response_types="code",
        token_endpoint_auth_method=auth_method,
        scope=scope_value or None,
    )

    client_secret = None
    if auth_method == "client_secret_post":
        client_secret = secrets.token_urlsafe(32)
        client.client_secret_hash = OAuthClient.hash_secret(client_secret)

    db.session.add(client)
    db.session.commit()

    response = {
        "client_id": client.client_id,
        "client_id_issued_at": int(_now_utc().timestamp()),
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
        "scope": scope_value,
    }
    if client_secret:
        response["client_secret"] = client_secret
        response["client_secret_expires_at"] = 0
    return jsonify(response), 201


@bp.route("/revoke", methods=["POST"])
def revoke():
    """RFC 7009 - トークン失効 (アクセス / リフレッシュトークン)。"""
    payload = request.get_json(silent=True) or request.form
    raw = payload.get("token")
    if raw:
        token = OAuthToken.query.filter_by(
            token_hash=OAuthToken.hash_token(raw)
        ).first()
        if token is None:
            token = OAuthToken.query.filter_by(
                refresh_token_hash=OAuthToken.hash_token(raw)
            ).first()
        if token is not None:
            token.is_active = False
            token.revoked_at = _now_utc()
            db.session.commit()
    # RFC 7009 では未知トークンでも 200 を返す
    return "", 200
