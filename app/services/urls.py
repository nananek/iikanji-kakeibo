"""外部公開 URL の基点を一元管理する。

OAuth ディスカバリやデバイス認証 URI など、クライアントへ返す絶対 URL を
リクエストの Host ヘッダから組み立てると、Host 偽装で応答 URL を attacker
ドメインに差し替えられ得る。設定済みの公開 URL（SERVER_NAME /
PUBLIC_BASE_URL / WEBAUTHN_ORIGIN）を優先し、Host 依存を避ける。
"""

from flask import current_app, request


def external_base_url() -> str:
    """スキーム込みの外部基点 URL を返す (末尾スラッシュ無し)。"""
    base = (current_app.config.get("PUBLIC_BASE_URL") or "").strip()
    if not base:
        server_name = current_app.config.get("SERVER_NAME")
        if server_name:
            scheme = current_app.config.get("PREFERRED_URL_SCHEME", "https")
            base = f"{scheme}://{server_name}"
    if not base:
        # WebAuthn の origin はアプリの正規オリジンと一致している前提。
        base = (current_app.config.get("WEBAUTHN_ORIGIN") or "").strip()
    if base:
        return base.rstrip("/")
    # 未設定時のフォールバック (開発時のみ想定)。
    return request.url_root.rstrip("/")
