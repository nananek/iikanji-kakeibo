"""#385 PR-2: ログイン派生 MK の 2 ラウンドログイン JSON API。

設計書 docs/v5-e2ee/login-derived-mk.md §3.2 / §3.5。

  POST /auth/login/begin  {username}
    → {salt, kdf_params, migration_required[, requires_password_setup]}
  POST /auth/login/finish
    通常パス : {username, login_verifier}
    移行パス : {username, password, login_verifier, login_salt, login_kdf_params,
               wrapped_master_key, wrap_iv}

JSON 専用のため CSRF 免除 (app/__init__.py で csrf.exempt)。レート制限は既存
ログイン系と同等の 10/minute。`/begin` は認証前に呼ばれる列挙の攻撃面なので、
未知ユーザーにも決定的ダミー salt を返し migration_required は常に false にする。

⚠️ 移行パスは werkzeug 検証のため平文 `password` を 1 回だけ受け取る (§2 原則の
例外)。`request.get_json()` の dict から `pop` してスコープを最小化し、エラー
ハンドラやアクセスログにボディを残さないこと (設計書 §3.5 PR-2 チェックリスト)。
"""

import hmac
import secrets
from base64 import b64decode, b64encode

from flask import Blueprint, jsonify, request, session
from flask_login import current_user, login_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db, limiter
from app.models.user import User
from app.models.wrapped_key import METHOD_PASSPHRASE, WrappedKey
from app.services import login_derived as ld
from app.services.migration_status import migration_rewrap_years


bp = Blueprint("auth_api", __name__, url_prefix="/auth/login")

# 移行用一時 salt を /begin と /finish の間で引き渡す session キー。
_SESSION_SALT = "pending_login_salt"   # b64(16B)
_SESSION_SALT_USER = "pending_login_salt_user"

LOGIN_SALT_LEN = 16
WRAP_IV_LEN = 12
MAX_WRAPPED_KEY_SIZE = 256  # AES-256-GCM wrapped MK は 48B 想定


def _b64e(raw):
    return b64encode(raw).decode("ascii")


def _b64d(value, expected_len=None):
    """base64 文字列を bytes に復号。型/長さ不正なら None。"""
    if not isinstance(value, str):
        return None
    try:
        decoded = b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None
    if expected_len is not None and len(decoded) != expected_len:
        return None
    return decoded


@bp.route("/begin", methods=["POST"])
@limiter.limit("10/minute")
def login_begin():
    """ログイン 1 ラウンド目。username に対し salt / kdf_params / migration_required を返す。"""
    if not ld.is_configured():
        return jsonify({"error": "login not configured"}), 503
    if current_user.is_authenticated:
        return jsonify({"error": "already authenticated"}), 400
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"error": "username required"}), 400

    user = User.query.filter_by(username=username, user_type="personal").first()
    kdf = ld.DEFAULT_KDF_PARAMS

    # 既移行ユーザー: 保存済み salt を返す (通常パス)。
    if user is not None and user.login_salt is not None:
        return jsonify({
            "salt": _b64e(bytes(user.login_salt)),
            "kdf_params": user.login_kdf_params or kdf,
            "migration_required": False,
        })

    # v4 移行対象 (password_hash 有 / login_salt 無): 新規 salt を発行し session に保持。
    if user is not None and user.password_hash:
        new_salt = secrets.token_bytes(LOGIN_SALT_LEN)
        session[_SESSION_SALT] = _b64e(new_salt)
        session[_SESSION_SALT_USER] = username
        return jsonify({
            "salt": _b64e(new_salt),
            "kdf_params": kdf,
            "migration_required": True,
        })

    # パスワード非保有 / 不正状態 (passkey_only など): パスワード設定を促す。
    if user is not None:
        return jsonify({
            "salt": _b64e(secrets.token_bytes(LOGIN_SALT_LEN)),
            "kdf_params": kdf,
            "migration_required": True,
            "requires_password_setup": True,
        })

    # 未知ユーザー: 決定的ダミー salt + migration_required:false (列挙耐性, §3.2)。
    return jsonify({
        "salt": _b64e(ld.compute_dummy_salt(username)),
        "kdf_params": kdf,
        "migration_required": False,
    })


@bp.route("/finish", methods=["POST"])
@limiter.limit("10/minute")
def login_finish():
    """ログイン 2 ラウンド目。通常パスと v4 移行パスを login_verifier で分岐する。"""
    if not ld.is_configured():
        return jsonify({"error": "login not configured"}), 503
    if current_user.is_authenticated:
        return jsonify({"error": "already authenticated"}), 400
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    login_verifier = _b64d(data.get("login_verifier"), 32)
    if not username or login_verifier is None:
        return jsonify({"error": "invalid request"}), 400

    user = User.query.filter_by(username=username, user_type="personal").first()

    # 移行パスは暗号材料 (wrapped_master_key) の有無で判定する。
    if "wrapped_master_key" in data:
        return _finish_migrate(user, username, login_verifier, data)
    return _finish_normal(user, login_verifier)


def _finish_normal(user, login_verifier):
    """既移行ユーザーの通常ログイン。login_verifier を HMAC 照合する。"""
    # 列挙/タイミング対策: 該当しない場合もダミー比較してから一律失敗を返す。
    if user is None or user.login_salt is None or not user.login_server_hash:
        ld.verify_login_verifier(b"\x00" * 32, login_verifier)
        return jsonify({"error": "authentication failed"}), 401
    if not ld.verify_login_verifier(user.login_server_hash, login_verifier):
        return jsonify({"error": "authentication failed"}), 401

    # 遅延 secret_version スタンプ (§3.1。現状は単一 secret なので version 同期のみ)。
    if user.login_secret_version != ld.CURRENT_SECRET_VERSION:
        user.login_secret_version = ld.CURRENT_SECRET_VERSION
        db.session.commit()

    locked = not user.is_active
    # is_active=False (鍵未設定ロック) は force=True で限定セッション (gate が制御)。
    login_user(user, remember=not locked, force=locked)
    return jsonify({"ok": True, "locked": locked})


def _finish_migrate(user, username, login_verifier, data):
    """v4 ユーザーの初回移行 finish。werkzeug 最終検証 + 認証因子確立。"""
    # 既に移行済み / 対象外は拒否 (login_salt 有 or password_hash 無)。
    if user is None or not user.password_hash or user.login_salt is not None:
        return jsonify({"error": "migration not applicable"}), 400

    # データ消失防止ガード: 既に wrapped_key を持つ (= 旧ウィザードで別パスフレーズ由来の
    # MK を確立済み) ユーザーは、ログインパスワードからその MK を再現できない。ここで新 MK を
    # 生成すると既存鍵が孤立し暗号化データが復号不能になるため、移行パスに通さない。
    # 真の v4 移行対象 (E2EE 未設定・temp-MK 暗号) は wrapped_key を持たない。
    if WrappedKey.query.filter_by(user_id=user.id).first() is not None:
        return jsonify({"error": "migration not applicable"}), 400

    # werkzeug を最終 1 回検証する。平文は dict から pop してスコープを最小化し、
    # ボディがログ/例外に残らないようにする (設計書 §3.5)。
    password = data.pop("password", None)
    ok_pw = isinstance(password, str) and user.check_password(password)
    del password
    if not ok_pw:
        return jsonify({"error": "authentication failed"}), 401

    # /begin で session に保持した一時 salt と一致確認する (改ざん・競合防止)。
    login_salt = _b64d(data.get("login_salt"), LOGIN_SALT_LEN)
    pending = _b64d(session.get(_SESSION_SALT), LOGIN_SALT_LEN)
    if (
        login_salt is None
        or pending is None
        or session.get(_SESSION_SALT_USER) != username
        or not hmac.compare_digest(login_salt, pending)
    ):
        return jsonify({"error": "invalid salt"}), 400

    # 暗号材料の検証。
    wrapped = _b64d(data.get("wrapped_master_key"))
    wrap_iv = _b64d(data.get("wrap_iv"), WRAP_IV_LEN)
    kdf = ld.validate_kdf_params(data.get("login_kdf_params"))
    if (
        wrapped is None
        or not (0 < len(wrapped) <= MAX_WRAPPED_KEY_SIZE)
        or wrap_iv is None
        or kdf is None
    ):
        return jsonify({"error": "invalid key material"}), 400

    # 単一トランザクションで認証因子確立 + passphrase wrapped_key UPSERT。
    # password_hash はまだ残す (rewrap 未完。finalize でクリア)。
    user.login_server_hash = ld.compute_login_server_hash(login_verifier)
    user.login_salt = login_salt
    user.login_kdf_params = kdf
    user.login_secret_version = ld.CURRENT_SECRET_VERSION

    wk = (
        WrappedKey.query.filter_by(user_id=user.id, method=METHOD_PASSPHRASE)
        .filter(WrappedKey.webauthn_credential_id.is_(None))
        .first()
    )
    if wk is None:
        wk = WrappedKey(user_id=user.id, method=METHOD_PASSPHRASE)
        db.session.add(wk)
    wk.wrapped_master_key = wrapped
    wk.wrap_iv = wrap_iv
    wk.salt = login_salt
    wk.kdf_params = kdf
    wk.label = "ログインパスワード"

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "conflict"}), 409

    # 一時 salt を破棄。
    session.pop(_SESSION_SALT, None)
    session.pop(_SESSION_SALT_USER, None)

    locked = not user.is_active
    login_user(user, remember=not locked, force=locked)
    needs_rewrap = user.migration_temp_mk is not None
    return jsonify({
        "ok": True,
        "migrated": True,
        "locked": locked,
        "user_id": user.id,
        # temp-MK が残っていれば、クライアントは続けて rewrap → finalize する。
        # years は透過 rewrap ドライバ (runRewrapMigration) の進捗計算に使う。
        "needs_rewrap": needs_rewrap,
        "years": migration_rewrap_years(user.id) if needs_rewrap else [],
    })
