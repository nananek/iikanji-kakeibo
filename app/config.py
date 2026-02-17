import os
from datetime import timedelta


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "postgresql://iikanji:iikanji@db:5432/iikanji"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = True

    # セッション永続化（明示的ログアウトまで維持）
    REMEMBER_COOKIE_DURATION = timedelta(days=365)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # CAPTCHA (optional)
    CAPTCHA_PROVIDER = os.environ.get("CAPTCHA_PROVIDER")       # hcaptcha|recaptcha|turnstile|mcaptcha
    CAPTCHA_SITE_KEY = os.environ.get("CAPTCHA_SITE_KEY")
    CAPTCHA_SECRET_KEY = os.environ.get("CAPTCHA_SECRET_KEY")
    CAPTCHA_API_URL = os.environ.get("CAPTCHA_API_URL")         # mCaptcha only

    # WebAuthn / Passkey
    WEBAUTHN_RP_ID = os.environ.get("WEBAUTHN_RP_ID", "localhost")
    WEBAUTHN_RP_NAME = os.environ.get("WEBAUTHN_RP_NAME", "いいかんじ™家計簿")
    WEBAUTHN_ORIGIN = os.environ.get("WEBAUTHN_ORIGIN", "http://localhost:5001")
