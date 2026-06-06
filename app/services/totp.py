"""#385 PR-T2: TOTP 2FA のサービス層 (設計書 §3.6)。

secret 生成・provisioning URI / QR (SVG)・コード検証・バックアップコード発行をまとめる。
secret のサーバ側 at-rest 暗号化は login_derived.encrypt/decrypt_totp_secret に委譲する。
"""

import base64
import hashlib
import secrets

import pyotp
import qrcode
import qrcode.image.svg

from app.extensions import db
from app.models.totp_backup_code import TotpBackupCode

ISSUER = "いいかんじ家計簿"
SECRET_LEN = 20          # RFC 4226 推奨 160bit
BACKUP_CODE_COUNT = 10
BACKUP_CODE_BYTES = 5    # token_hex(5) = 10 hex 桁
VALID_WINDOW = 1         # ±1 step (時刻ずれ許容)


def generate_secret_bytes():
    """TOTP secret (20B) を生成する。"""
    return secrets.token_bytes(SECRET_LEN)


def secret_to_base32(secret_bytes):
    """pyotp / authenticator アプリが要求する base32 文字列に変換する。"""
    return base64.b32encode(bytes(secret_bytes)).decode("ascii")


def provisioning_uri(secret_bytes, username):
    """otpauth:// URI を返す (authenticator アプリ登録用)。"""
    return pyotp.TOTP(secret_to_base32(secret_bytes)).provisioning_uri(
        name=username, issuer_name=ISSUER
    )


def qr_svg(uri):
    """otpauth URI の QR コードを SVG 文字列で返す (Pillow 非依存)。

    SvgPathImage は QR モジュールを path で描くだけで、URI 中の文字 (username 等) を
    テキストとして埋め込まないため、そのまま inline しても XSS 面はない。
    """
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    return img.to_string().decode("ascii")


def verify_code(secret_bytes, code):
    """6 桁 TOTP コードを検証する (±1 step 許容)。

    @returns bool
    """
    if not code or not isinstance(code, str):
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return False
    return pyotp.TOTP(secret_to_base32(secret_bytes)).verify(code, valid_window=VALID_WINDOW)


def _hash_backup_code(raw):
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def generate_backup_codes(user_id):
    """バックアップコード (10 桁 hex × 10) を生成して保存する (旧コードは全削除)。

    平文コードのリストを返す (発行時 1 回だけ表示用)。呼び出し側で commit する。
    DELETE + INSERT は同一トランザクション内 (呼び出し側 commit) で行い、途中障害でも
    コードが 0 件にならない (§3.6.3)。
    """
    TotpBackupCode.query.filter_by(user_id=user_id).delete()
    codes = []
    for _ in range(BACKUP_CODE_COUNT):
        raw = secrets.token_hex(BACKUP_CODE_BYTES)
        codes.append(raw)
        db.session.add(TotpBackupCode(
            user_id=user_id,
            code_hash=_hash_backup_code(raw),
            code_prefix=raw[:4] + "...",
        ))
    return codes


def consume_backup_code(user_id, raw):
    """バックアップコードを 1 回限り消費する (#385 PR-T3 で login finish が使用)。

    定数時間照合のため全未使用コードと compare_digest し、一致した未使用コードに
    used_at をセットする。

    @returns bool  消費成功なら True
    """
    import hmac as _hmac
    from datetime import datetime, timezone

    if not raw or not isinstance(raw, str):
        return False
    target = _hash_backup_code(raw.strip())
    rows = TotpBackupCode.query.filter_by(user_id=user_id, used_at=None).all()
    matched = None
    for row in rows:
        if _hmac.compare_digest(row.code_hash, target):
            matched = row
    if matched is None:
        return False
    matched.used_at = datetime.now(timezone.utc)
    return True
