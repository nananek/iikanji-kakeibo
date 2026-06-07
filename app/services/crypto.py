"""アプリ共通の at-rest 暗号化ヘルパー。

`SECRET_KEY` から導出した Fernet 鍵で任意のバイト列を暗号化/復号する。
AI API キー (`ai_receipt.encrypt_api_key`) や TOTP secret (`totp`) など、
サーバー側で復号できれば十分なデータの保管に使う。

注意: `SECRET_KEY` を変更すると既存の暗号文は復号不能になる。
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def get_fernet() -> Fernet:
    """SECRET_KEY から Fernet インスタンスを生成する。"""
    secret = current_app.config["SECRET_KEY"]
    key_bytes = hashlib.sha256(secret.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt_bytes(plain: bytes) -> bytes:
    """バイト列を暗号化する。"""
    return get_fernet().encrypt(plain)


def decrypt_bytes(token: bytes) -> bytes:
    """暗号化バイト列を復号する。SECRET_KEY 変更時は InvalidToken。"""
    return get_fernet().decrypt(token)


__all__ = ["get_fernet", "encrypt_bytes", "decrypt_bytes", "InvalidToken"]
