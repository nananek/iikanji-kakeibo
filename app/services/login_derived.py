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
import os

from flask import current_app

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

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
# #385 PR-4b: リカバリシードをフル復旧因子化する verifier 用ラベル (§3.4.1)。
_RECOVERY_HASH_LABEL = b"recovery-hash\x00"
# PR-4b-2: /auth/recovery/begin の列挙耐性ダミー応答用ラベル (§3.4.1)。
_DUMMY_RECOVERY_WRAP_LABEL = b"dummy-recovery-wrap\x00"
_DUMMY_RECOVERY_WRAP2_LABEL = b"dummy-recovery-wrap2\x00"
_DUMMY_RECOVERY_IV_LABEL = b"dummy-recovery-iv\x00"
# PR-T1: TOTP secret の at-rest 暗号鍵を導出する HKDF info (§3.6.1)。
_TOTP_ENC_INFO = b"iikanji-totp-enc-v1"


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
    """送られた login_verifier が保存ハッシュと一致するか定数時間比較する。

    注: ここは stored_hash NULL で**意図的に早期 return** する。login の NULL は
    「未移行ユーザー」を意味し、その有無は §3.2 のダミー salt で別途吸収しているため
    タイミングで漏れても問題ない。一方 `verify_recovery_verifier` は NULL が
    「シード未設定」を意味し、その有無を漏らしてはならないので早期 return しない (§3.4.1)。
    """
    if not stored_hash:
        return False
    computed = compute_login_server_hash(login_verifier)
    return hmac.compare_digest(computed, bytes(stored_hash))


def compute_recovery_server_hash(recovery_verifier, *, secret=None):
    """recovery_seed_server_hash = HMAC-SHA256(secret, "recovery-hash"||0x00||recovery_verifier)。

    recovery_verifier は HKDF(seed_bytes, info="iikanji-recovery-login-v1") の 32B
    (クライアントが計算して送る)。login_server_hash と同じく生の verifier は保存せず、
    本ハッシュのみ DB に置く (§3.4.1)。

    @param recovery_verifier  クライアントが送る 32B (bytes)
    @returns 32B HMAC ダイジェスト (bytes)
    """
    if secret is None:
        secret = _secret_bytes()
    return hmac.new(
        secret, _RECOVERY_HASH_LABEL + recovery_verifier, hashlib.sha256
    ).digest()


def verify_recovery_verifier(stored_hash, recovery_verifier):
    """recovery_verifier が保存ハッシュと一致するか定数時間照合する (§3.4.1)。

    stored_hash が NULL (旧ウィザード作成 / シード未設定) でも `0x00 * 32` との
    ダミー照合を**常に実行してから** False を返す。`if stored_hash is None: return`
    の早期 return はタイミングでシード設定有無を漏らすため使わない。
    """
    computed = compute_recovery_server_hash(recovery_verifier)
    reference = bytes(stored_hash) if stored_hash else b"\x00" * 32
    matched = hmac.compare_digest(computed, reference)
    # NULL の場合は (理論上 computed が全ゼロでも) 認証成立させない。
    return matched and bool(stored_hash)


def compute_dummy_recovery_wrap(username):
    """/auth/recovery/begin の列挙耐性ダミー応答 (wrapped_master_key 48B, wrap_iv 12B)。

    実値 (AES-256-GCM: ciphertext 32B + GCM tag 16B = 48B / IV 12B) と**長さを一致**させ、
    username に対し**決定的**にすることで、シード設定有無を「長さ」でも「2 回叩いて値が
    変わる差」でも漏らさない (§3.4.1)。ダミーで unwrap しても finish の verifier 照合で
    必ず失敗する。

    @returns (wrapped_master_key: 48 bytes, wrap_iv: 12 bytes)
    """
    secret = _secret_bytes()
    uname = (username or "").encode("utf-8")
    raw = hmac.new(
        secret, _DUMMY_RECOVERY_WRAP_LABEL + uname, hashlib.sha256
    ).digest()  # 32B
    ext = hmac.new(
        secret, _DUMMY_RECOVERY_WRAP2_LABEL + uname, hashlib.sha256
    ).digest()[:16]  # 16B
    iv = hmac.new(
        secret, _DUMMY_RECOVERY_IV_LABEL + uname, hashlib.sha256
    ).digest()[:12]  # 12B
    return raw + ext, iv


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


def _totp_enc_key():
    """TOTP secret の at-rest 暗号鍵 = HKDF-SHA256(LOGIN_SERVER_SECRET, salt=zero(32),
    info="iikanji-totp-enc-v1", L=32) (§3.6.1)。

    salt=zero(32) は意図的 — LOGIN_SERVER_SECRET 自体が高エントロピー鍵素材なので可変
    salt は不要 (RFC 5869 §2.2)。login_verifier / recovery のドメインと info で分離する。
    """
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"\x00" * 32, info=_TOTP_ENC_INFO,
    ).derive(_secret_bytes())


def encrypt_totp_secret(secret_bytes, user_id):
    """TOTP secret をサーバ鍵で AES-256-GCM 暗号化する (§3.6.1)。

    AAD に user_id を含めることで別ユーザーへの暗号文移植を防ぐ。AAD は
    `str(user_id).encode("ascii")` に固定 (環境非依存)。

    @returns (ciphertext+tag: bytes, iv: 12 bytes)
    """
    key = _totp_enc_key()
    iv = os.urandom(12)
    aad = str(int(user_id)).encode("ascii")
    ciphertext = AESGCM(key).encrypt(iv, secret_bytes, aad)
    return ciphertext, iv


def decrypt_totp_secret(ciphertext, iv, user_id):
    """encrypt_totp_secret の逆。改ざん/別ユーザー移植は InvalidTag で失敗する。

    @returns secret_bytes
    """
    key = _totp_enc_key()
    aad = str(int(user_id)).encode("ascii")
    return AESGCM(key).decrypt(bytes(iv), bytes(ciphertext), aad)


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
