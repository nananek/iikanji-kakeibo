"""#385 ログイン派生 MK: サーバ側の検証ヘルパー。

設計書 docs/v5-e2ee/login-derived-mk.md §2 / §3。

クライアントは `master = Argon2id(password, salt)` から
`login_verifier = HKDF(master, "iikanji-login-v1")` を導出してサーバへ送る。
サーバは生の `login_verifier` を保存せず、本用途専用の `LOGIN_SERVER_SECRET` で
HMAC した `login_server_hash` を保存・照合する。DB が流出しても秘密が無ければ
`login_verifier` 平文 (照合値) を得られない。

`LOGIN_SERVER_SECRET` は HMAC の鍵として 2 用途で使い、メッセージ先頭にドメイン
ラベル + 0x00 を付けて分離する (設計書「LOGIN_SERVER_SECRET の用途分離」):
  - ログイン検証ハッシュ : "login-hash" || 0x00 || login_verifier
  - 列挙耐性ダミー salt   : "dummy-salt" || 0x00 || username
"""

import hashlib
import hmac

from flask import current_app

# Argon2id 既定パラメータ (client argon2.js の ARGON2ID_DEFAULTS と一致。確定値)。
DEFAULT_KDF_PARAMS = {"memory": 65536, "iterations": 3, "parallelism": 1}

# LOGIN_SERVER_SECRET の現行バージョン。秘密差し替え時にインクリメントし、
# login_secret_version が古い行をログイン成功時に遅延再計算する (§3.1)。
CURRENT_SECRET_VERSION = 1

# kdf_params のクライアント側下限 (改ざん多層防御)。サーバが返す値も最低これを満たす。
MIN_KDF_MEMORY = 65536
MIN_KDF_ITERATIONS = 3

_LOGIN_HASH_LABEL = b"login-hash\x00"
_DUMMY_SALT_LABEL = b"dummy-salt\x00"


def _secret_bytes():
    """LOGIN_SERVER_SECRET を bytes で返す。未設定なら空 bytes (呼び出し側が判定)。"""
    return (current_app.config.get("LOGIN_SERVER_SECRET", "") or "").encode("utf-8")


def is_configured():
    """LOGIN_SERVER_SECRET が設定されているか。"""
    return bool(_secret_bytes())


def compute_login_server_hash(login_verifier, *, secret=None):
    """login_server_hash = HMAC-SHA256(secret, "login-hash" || 0x00 || login_verifier)。

    @param login_verifier  クライアントが送る 32B (bytes)
    @returns 32B HMAC ダイジェスト (bytes)
    """
    if secret is None:
        secret = _secret_bytes()
    return hmac.new(secret, _LOGIN_HASH_LABEL + login_verifier, hashlib.sha256).digest()


def verify_login_verifier(stored_hash, login_verifier):
    """送られた login_verifier が保存ハッシュと一致するか定数時間比較する。"""
    if not stored_hash:
        return False
    computed = compute_login_server_hash(login_verifier)
    return hmac.compare_digest(computed, bytes(stored_hash))


def compute_dummy_salt(username):
    """未知ユーザー用の決定的ダミー salt (16B)。

    HMAC-SHA256(secret, "dummy-salt" || 0x00 || username)[0:16]。username に対し
    決定的にすることで「同名 2 回で salt が変わる」差による存在判定を防ぐ (§3.2)。
    """
    digest = hmac.new(
        _secret_bytes(), _DUMMY_SALT_LABEL + (username or "").encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return digest[:16]


def validate_kdf_params(params):
    """kdf_params が最低強度を満たすか検証する (弱化パラメータ拒否)。

    @returns 正常な dict、または None (不正)。
    """
    if not isinstance(params, dict):
        return None
    try:
        memory = int(params.get("memory"))
        iterations = int(params.get("iterations"))
        parallelism = int(params.get("parallelism"))
    except (TypeError, ValueError):
        return None
    if memory < MIN_KDF_MEMORY or iterations < MIN_KDF_ITERATIONS or parallelism < 1:
        return None
    return {"memory": memory, "iterations": iterations, "parallelism": parallelism}
