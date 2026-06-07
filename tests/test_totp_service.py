"""TOTP サービス (app/services/totp.py) のユニットテスト。"""

import base64

import pyotp
import pytest

from app.services import totp as totp_svc


def test_generate_secret_is_valid_base32(app):
    with app.app_context():
        secret = totp_svc.generate_secret()
    # 20 バイト (160bit) → base32 で 32 文字 (パディング除去)
    assert len(secret) == 32
    # base32 デコードできる (パディング補完して検証)
    pad = "=" * (-len(secret) % 8)
    assert len(base64.b32decode(secret + pad)) == 20


def test_encrypt_decrypt_roundtrip(app):
    with app.app_context():
        secret = totp_svc.generate_secret()
        token = totp_svc.encrypt_secret(secret)
        assert isinstance(token, bytes)
        assert token != secret.encode()
        assert totp_svc.decrypt_secret(token) == secret


def test_decrypt_with_changed_secret_key_raises(app):
    with app.app_context():
        token = totp_svc.encrypt_secret(totp_svc.generate_secret())
    # SECRET_KEY を変えると復号不能 → ValueError
    app.config["SECRET_KEY"] = "a-totally-different-secret-key"
    with app.app_context():
        with pytest.raises(ValueError):
            totp_svc.decrypt_secret(token)


def test_provisioning_uri_contains_issuer(app):
    with app.app_context():
        secret = totp_svc.generate_secret()
        uri = totp_svc.provisioning_uri(secret, "alice")
    assert uri.startswith("otpauth://totp/")
    assert "alice" in uri
    # issuer は URL エンコードされる
    assert "issuer=" in uri


def test_qr_svg_returns_svg(app):
    with app.app_context():
        uri = totp_svc.provisioning_uri(totp_svc.generate_secret(), "alice")
        svg = totp_svc.qr_svg(uri)
    assert "<svg" in svg


def test_verify_code_accepts_current_rejects_wrong(app):
    with app.app_context():
        secret = totp_svc.generate_secret()
        code = pyotp.TOTP(secret).now()
        assert totp_svc.verify_code(secret, code) is True
        assert totp_svc.verify_code(secret, "000000") is False
        assert totp_svc.verify_code(secret, "abc") is False
        assert totp_svc.verify_code(secret, "12345") is False  # 桁数不足


def test_verify_code_normalizes_spaces(app):
    with app.app_context():
        secret = totp_svc.generate_secret()
        code = pyotp.TOTP(secret).now()
        spaced = code[:3] + " " + code[3:]
        assert totp_svc.verify_code(secret, spaced) is True


def test_verify_code_with_step_replay_protection(app):
    with app.app_context():
        secret = totp_svc.generate_secret()
        at = 1_700_000_000  # 固定時刻
        code = pyotp.TOTP(secret).at(at)
        step = totp_svc.current_step(at)

        # 初回 (last_used_step=None) は受理し、一致 step を返す
        ok, matched = totp_svc.verify_code_with_step(secret, code, None, at=at)
        assert ok is True
        assert matched == step

        # 同一 step の再利用は拒否 (リプレイ)
        ok2, matched2 = totp_svc.verify_code_with_step(
            secret, code, last_used_step=step, at=at
        )
        assert ok2 is False
        assert matched2 is None

        # 1 つ前の step が消費済みでも、今の step は受理
        ok3, matched3 = totp_svc.verify_code_with_step(
            secret, code, last_used_step=step - 1, at=at
        )
        assert ok3 is True
        assert matched3 == step


def test_verify_code_with_step_window_tolerance(app):
    with app.app_context():
        secret = totp_svc.generate_secret()
        at = 1_700_000_000
        prev_step = totp_svc.current_step(at) - 1
        prev_code = pyotp.TOTP(secret).at(prev_step * 30)
        # ±1 step の許容で 1 つ前のコードも受理される
        ok, matched = totp_svc.verify_code_with_step(secret, prev_code, None, at=at)
        assert ok is True
        assert matched == prev_step


def test_verify_code_with_step_rejects_non_digit(app):
    with app.app_context():
        secret = totp_svc.generate_secret()
        ok, matched = totp_svc.verify_code_with_step(secret, "abcdef", None)
        assert ok is False
        assert matched is None
