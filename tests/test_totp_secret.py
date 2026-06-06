"""#385 PR-T1: TOTP secret の at-rest 暗号化ヘルパーのテスト (設計書 §3.6.1)。"""

import pytest
from cryptography.exceptions import InvalidTag

from app.services import login_derived as ld


LOGIN_SECRET = "test-login-server-secret"


@pytest.fixture(autouse=True)
def _login_secret(app):
    prev = app.config.get("LOGIN_SERVER_SECRET", "")
    app.config["LOGIN_SERVER_SECRET"] = LOGIN_SECRET
    yield
    app.config["LOGIN_SERVER_SECRET"] = prev


def test_encrypt_decrypt_roundtrip(app):
    with app.app_context():
        secret = b"\x01" * 20  # RFC 4226 推奨 160bit
        ct, iv = ld.encrypt_totp_secret(secret, user_id=42)
        assert len(iv) == 12
        assert len(ct) == 20 + 16  # 平文 20B + GCM tag 16B = 36B
        assert ct != secret
        assert ld.decrypt_totp_secret(ct, iv, user_id=42) == secret


def test_decrypt_wrong_user_id_fails(app):
    """AAD=user_id 不一致 (別ユーザーへの暗号文移植) は InvalidTag で失敗する。"""
    with app.app_context():
        ct, iv = ld.encrypt_totp_secret(b"\x02" * 20, user_id=1)
        with pytest.raises(InvalidTag):
            ld.decrypt_totp_secret(ct, iv, user_id=2)


def test_decrypt_tampered_ciphertext_fails(app):
    with app.app_context():
        ct, iv = ld.encrypt_totp_secret(b"\x03" * 20, user_id=7)
        tampered = bytes([ct[0] ^ 0xFF]) + ct[1:]
        with pytest.raises(InvalidTag):
            ld.decrypt_totp_secret(tampered, iv, user_id=7)


def test_iv_is_random_per_call(app):
    """同一 secret/ユーザーでも IV が毎回変わる (決定的でない)。"""
    with app.app_context():
        ct1, iv1 = ld.encrypt_totp_secret(b"\x04" * 20, user_id=9)
        ct2, iv2 = ld.encrypt_totp_secret(b"\x04" * 20, user_id=9)
        assert iv1 != iv2
        assert ct1 != ct2
        # どちらも復号できる
        assert ld.decrypt_totp_secret(ct1, iv1, 9) == b"\x04" * 20
        assert ld.decrypt_totp_secret(ct2, iv2, 9) == b"\x04" * 20


def test_enc_key_domain_separated(app):
    """totp_enc_key は LOGIN_SERVER_SECRET 由来だが他用途 (login/recovery HMAC) とは別物。
    別 secret では復号できない (鍵が変わる)。"""
    with app.app_context():
        ct, iv = ld.encrypt_totp_secret(b"\x05" * 20, user_id=3)
    # secret を変えると復号不可
    with app.app_context():
        app.config["LOGIN_SERVER_SECRET"] = "different-secret"
        with pytest.raises(InvalidTag):
            ld.decrypt_totp_secret(ct, iv, user_id=3)
