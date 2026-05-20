"""CAPTCHA token verification"""

import httpx
from flask import current_app, flash, request


# Provider -> (verify URL, response form field name)
_PROVIDERS = {
    "hcaptcha": ("https://api.hcaptcha.com/siteverify", "h-captcha-response"),
    "recaptcha": ("https://www.google.com/recaptcha/api/siteverify", "g-recaptcha-response"),
    "turnstile": ("https://challenges.cloudflare.com/turnstile/v0/siteverify", "cf-turnstile-response"),
    "mcaptcha": (None, "mcaptcha__token"),  # URL from CAPTCHA_API_URL
}


def is_captcha_enabled() -> bool:
    provider = current_app.config.get("CAPTCHA_PROVIDER")
    return bool(provider and provider in _PROVIDERS)


def get_captcha_response_field() -> str | None:
    """Return the form field name for the current provider, or None if disabled."""
    provider = current_app.config.get("CAPTCHA_PROVIDER")
    if not provider:
        return None
    info = _PROVIDERS.get(provider)
    return info[1] if info else None


def verify_captcha_token(token: str) -> bool:
    """Verify a CAPTCHA response token. Returns True if valid."""
    provider = current_app.config.get("CAPTCHA_PROVIDER")
    secret = current_app.config.get("CAPTCHA_SECRET_KEY")

    if not provider or not secret:
        return True  # not configured = pass

    info = _PROVIDERS.get(provider)
    if not info:
        current_app.logger.warning("Unknown CAPTCHA provider: %s", provider)
        return False

    verify_url = info[0]
    if provider == "mcaptcha":
        verify_url = current_app.config.get("CAPTCHA_API_URL")
        if not verify_url:
            current_app.logger.warning("CAPTCHA_API_URL required for mCaptcha")
            return False

    try:
        resp = httpx.post(verify_url, data={
            "secret": secret,
            "response": token,
        }, timeout=5.0)
        result = resp.json()
        return result.get("success", False)
    except Exception as e:
        current_app.logger.error("CAPTCHA verification failed: %s", e)
        return False


def check_captcha_or_flash() -> bool:
    """CAPTCHA 検証に失敗したら flash して False を返す。未設定環境では常に True。"""
    if not is_captcha_enabled():
        return True
    field = get_captcha_response_field()
    token = request.form.get(field, "")
    if not token or not verify_captcha_token(token):
        flash("CAPTCHA 認証に失敗しました。もう一度お試しください。", "danger")
        return False
    return True
