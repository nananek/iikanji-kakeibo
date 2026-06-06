"""#385 PR-4b-2: リカバリシードによるパスワードリセット API。

設計書 docs/v5-e2ee/login-derived-mk.md §3.4.1。リカバリシードを「フル復旧因子」化し、
パスワードを忘れたユーザーがシード単体でリセットできるようにする。

  POST /auth/recovery/begin  {username}
    → {wrapped_master_key, wrap_iv}  (recovery_seed wrapped_key を返し MK unwrap させる)
  POST /auth/recovery/finish
    {username, recovery_verifier,                        # 旧シード由来・認証用
     login_salt, login_verifier, login_kdf_params,       # 新パスワード由来
     passphrase_wrapped_master_key, passphrase_wrap_iv,  # MK を新PWで wrap
     recovery_wrapped_master_key, recovery_wrap_iv,      # MK を新シードで wrap (ローテ)
     new_recovery_verifier}                              # 新シード由来
    → {ok: True}

JSON 専用のため CSRF 免除 (app/__init__.py)。未認証で叩く攻撃面なので:
- begin は DB hit/miss・NULL hash・wrapped_key 欠如のいずれでも常に DB lookup と
  ダミー HMAC 計算を実行し、実値と同じ長さ (48B/12B) の応答を返す (列挙/タイミング耐性)。
- finish は recovery_seed_server_hash が NULL でも `0x00*32` とのダミー照合を必ず実行して
  から失敗する (login_derived.verify_recovery_verifier が担保)。
- TOTP はリセットではバイパスする (seed = 全権復旧因子。設計判断 2026-06-07)。
"""

from base64 import b64decode, b64encode

from flask import Blueprint, jsonify, request
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db, limiter
from app.models.user import User
from app.models.wrapped_key import (
    METHOD_PASSPHRASE,
    METHOD_RECOVERY_SEED,
    WrappedKey,
)
from app.services import login_derived as ld


bp = Blueprint("auth_recovery", __name__, url_prefix="/auth/recovery")

LOGIN_SALT_LEN = 16
WRAP_IV_LEN = 12
VERIFIER_LEN = 32
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


def _username_rate_key():
    """per-username レート制限のキー (リクエストボディの username 由来)。"""
    data = request.get_json(silent=True) or {}
    return "recovery:" + (data.get("username") or "").strip().lower()


@bp.route("/begin", methods=["POST"])
@limiter.limit("5/minute")                                # per-IP
@limiter.limit("10/hour", key_func=_username_rate_key)    # per-username
def recovery_begin():
    """リセット 1 ラウンド目。recovery_seed wrapped_key を返し MK を unwrap させる。

    列挙/タイミング耐性 (§3.4.1):
    - username 不在 / recovery_seed_server_hash が NULL / recovery_seed wrapped_key 無し
      の全ケースで、username 由来の**決定的ダミー** (48B/12B) を返す。
    - DB lookup とダミー HMAC 計算は分岐に関わらず**常に実行**し、値の差し替えのみで分岐する。
    """
    if not ld.is_configured():
        return jsonify({"error": "recovery not configured"}), 503
    if current_user.is_authenticated:
        return jsonify({"error": "already authenticated"}), 400
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"error": "username required"}), 400

    # 常に DB lookup する (不在でも SELECT を投げ、hit/miss のレイテンシ差を消す)。
    user = User.query.filter_by(username=username, user_type="personal").first()
    # 常に recovery_seed wrapped_key を lookup する (user 不在でも一致しない id で投げる)。
    wk = (
        WrappedKey.query.filter_by(
            user_id=(user.id if user is not None else 0),
            method=METHOD_RECOVERY_SEED,
        ).first()
    )
    # 常にダミーを計算する (実値が使えるか否かは計算後に選択する = 早期 return 禁止)。
    dummy_wrapped, dummy_iv = ld.compute_dummy_recovery_wrap(username)

    real = (
        user is not None
        and user.recovery_seed_server_hash is not None
        and wk is not None
        and wk.wrapped_master_key is not None
        and wk.wrap_iv is not None
    )
    if real:
        wrapped, wrap_iv = bytes(wk.wrapped_master_key), bytes(wk.wrap_iv)
    else:
        wrapped, wrap_iv = dummy_wrapped, dummy_iv

    return jsonify({
        "wrapped_master_key": _b64e(wrapped),
        "wrap_iv": _b64e(wrap_iv),
    })


@bp.route("/finish", methods=["POST"])
@limiter.limit("5/minute")                               # per-IP
@limiter.limit("5/hour", key_func=_username_rate_key)    # per-username (verifier 総当たり抑止)
def recovery_finish():
    """リセット 2 ラウンド目。旧 recovery_verifier を照合し、新 PW・新シードを確立する。

    成功時に単一トランザクションで login_* / passphrase wrapped_key / recovery_seed
    wrapped_key / recovery_seed_server_hash を更新し、session_token_version を
    インクリメントして既存セッションを全失効させる (§3.4.1)。セッションは張らず、
    ユーザーは新パスワードで改めてログインする。
    """
    if not ld.is_configured():
        return jsonify({"error": "recovery not configured"}), 503
    if current_user.is_authenticated:
        return jsonify({"error": "already authenticated"}), 400
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()

    recovery_verifier = _b64d(data.get("recovery_verifier"), VERIFIER_LEN)
    new_recovery_verifier = _b64d(data.get("new_recovery_verifier"), VERIFIER_LEN)
    login_verifier = _b64d(data.get("login_verifier"), VERIFIER_LEN)
    login_salt = _b64d(data.get("login_salt"), LOGIN_SALT_LEN)
    kdf = ld.validate_kdf_params(data.get("login_kdf_params"))
    pp_wrapped = _b64d(data.get("passphrase_wrapped_master_key"))
    pp_iv = _b64d(data.get("passphrase_wrap_iv"), WRAP_IV_LEN)
    rec_wrapped = _b64d(data.get("recovery_wrapped_master_key"))
    rec_iv = _b64d(data.get("recovery_wrap_iv"), WRAP_IV_LEN)

    if (
        not username
        or recovery_verifier is None
        or new_recovery_verifier is None
        or login_verifier is None
        or login_salt is None
        or kdf is None
        or pp_wrapped is None
        or not (0 < len(pp_wrapped) <= MAX_WRAPPED_KEY_SIZE)
        or pp_iv is None
        or rec_wrapped is None
        or not (0 < len(rec_wrapped) <= MAX_WRAPPED_KEY_SIZE)
        or rec_iv is None
    ):
        return jsonify({"error": "invalid request"}), 400

    user = User.query.filter_by(username=username, user_type="personal").first()

    # 旧 recovery_verifier を定数時間照合する。user 不在 / hash NULL でも
    # verify_recovery_verifier が 0x00*32 とのダミー照合を必ず実行してから False を返す
    # (早期 return せず、シード設定有無をタイミングで漏らさない、§3.4.1)。
    stored = user.recovery_seed_server_hash if user is not None else None
    if not ld.verify_recovery_verifier(stored, recovery_verifier):
        return jsonify({"error": "recovery failed"}), 401

    # --- ここから認証成立。単一トランザクションで更新 (新保存完了後に旧シード無効化) ---
    user.login_server_hash = ld.compute_login_server_hash(login_verifier)
    user.login_salt = login_salt
    user.login_kdf_params = kdf
    user.login_secret_version = ld.CURRENT_SECRET_VERSION

    # passphrase wrapped_key を新 PW 由来で UPSERT。
    pp = (
        WrappedKey.query.filter_by(user_id=user.id, method=METHOD_PASSPHRASE)
        .filter(WrappedKey.webauthn_credential_id.is_(None))
        .first()
    )
    if pp is None:
        pp = WrappedKey(user_id=user.id, method=METHOD_PASSPHRASE)
        db.session.add(pp)
    pp.wrapped_master_key = pp_wrapped
    pp.wrap_iv = pp_iv
    pp.salt = login_salt
    pp.kdf_params = kdf
    pp.label = "ログインパスワード"

    # recovery_seed wrapped_key を新シード由来で UPSERT (シードローテーション)。
    rec = (
        WrappedKey.query.filter_by(user_id=user.id, method=METHOD_RECOVERY_SEED)
        .first()
    )
    if rec is None:
        rec = WrappedKey(user_id=user.id, method=METHOD_RECOVERY_SEED)
        db.session.add(rec)
    rec.wrapped_master_key = rec_wrapped
    rec.wrap_iv = rec_iv
    rec.salt = None
    rec.kdf_params = None
    rec.label = "リカバリシード"

    # 新シードの recovery_seed_server_hash を保存 (生 verifier は保存しない)。
    user.recovery_seed_server_hash = ld.compute_recovery_server_hash(
        new_recovery_verifier
    )

    # 既存セッションを全失効 (攻撃者が握る旧 Cookie / 解錠済み MK を無効化、§3.4.1)。
    user.bump_session_token_version()

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "conflict"}), 409

    # セッションは張らない。クライアントは新シードを表示後、新パスワードでログインし直す。
    return jsonify({"ok": True})
