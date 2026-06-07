"""TOTP (RFC 6238) 2要素認証サービス。

- secret は 160bit を base32 で保持し、`SECRET_KEY` 由来の Fernet で
  暗号化して `User.totp_secret_encrypted` に保管する (crypto.py 再利用)。
- ログイン時の検証は `verify_code_with_step()` を使い、消費済み step を
  `User.totp_last_used_step` に記録してリプレイ (同一コード再利用) を防ぐ。
- バックアップコードは持たない (パスキーログインが復旧経路を担う)。
"""

import base64
import hmac
import secrets
import time

import pyotp
import qrcode
import qrcode.image.svg

from app.services.crypto import encrypt_bytes, decrypt_bytes, InvalidToken

ISSUER_NAME = "いいかんじ家計簿"
_STEP_SECONDS = 30


def generate_secret() -> str:
    """160bit のランダム secret を base32 文字列で返す (パディング除去)。"""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def encrypt_secret(secret_b32: str) -> bytes:
    """base32 secret を暗号化する。"""
    return encrypt_bytes(secret_b32.encode("ascii"))


def decrypt_secret(token: bytes) -> str:
    """暗号化済み secret を復号する。SECRET_KEY 変更時は ValueError。"""
    try:
        return decrypt_bytes(bytes(token)).decode("ascii")
    except InvalidToken:
        raise ValueError(
            "TOTP secret の復号に失敗しました。SECRET_KEY が変更された"
            "可能性があります。二段階認証を再設定してください。"
        )


def provisioning_uri(secret_b32: str, account_name: str) -> str:
    """認証アプリ登録用の otpauth:// URI を生成する。"""
    return pyotp.TOTP(secret_b32).provisioning_uri(
        name=account_name, issuer_name=ISSUER_NAME
    )


def qr_svg(uri: str) -> str:
    """otpauth URI を SVG 文字列で返す (Pillow 非依存)。"""
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    return img.to_string(encoding="unicode")


def _normalize(code: str) -> str:
    return (code or "").strip().replace(" ", "").replace("-", "")


def verify_code(secret_b32: str, code: str, valid_window: int = 1) -> bool:
    """6桁 TOTP を検証する (登録確認用、リプレイ追跡なし)。"""
    code = _normalize(code)
    if not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret_b32).verify(code, valid_window=valid_window)


def current_step(at: float | None = None) -> int:
    """現在の TOTP step = floor(unixtime / 30) を返す。"""
    t = at if at is not None else time.time()
    return int(t) // _STEP_SECONDS


def verify_code_with_step(
    secret_b32: str,
    code: str,
    last_used_step: int | None,
    valid_window: int = 1,
    at: float | None = None,
) -> tuple[bool, int | None]:
    """ログイン時の TOTP 検証 (リプレイ防止付き)。

    返り値 (ok, matched_step)。一致した step が `last_used_step` 以下なら
    リプレイとして (False, None) を返す。呼び出し側は成功時に
    `user.totp_last_used_step = matched_step` を保存すること。
    """
    code = _normalize(code)
    if not code.isdigit() or len(code) != 6:
        return (False, None)
    totp = pyotp.TOTP(secret_b32)
    cur = current_step(at)
    for offset in range(-valid_window, valid_window + 1):
        step = cur + offset
        candidate = totp.at(step * _STEP_SECONDS)
        if hmac.compare_digest(candidate, code):
            if last_used_step is not None and step <= last_used_step:
                return (False, None)
            return (True, step)
    return (False, None)
