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

    # レート制限
    RATELIMIT_ENABLED = os.environ.get("RATELIMIT_ENABLED", "true").lower() != "false"

    # 新規登録の有効/無効。セルフホストで自家用に限定したい場合は "false" に設定。
    # false の場合、/register と /register/auditor は 404 を返し、
    # ログイン画面の「新規登録」リンクも非表示になる。既存ユーザーのログインは影響なし。
    REGISTRATION_ENABLED = os.environ.get("REGISTRATION_ENABLED", "true").lower() != "false"

    # エンタイトルメント基盤の動作モード。
    # - "unlimited" (default): セルフホスト前提で全有償機能を解放。
    #   billing コンテナへの HTTP リクエストは発生しない。
    # - "http": billing コンテナに HTTP 照会 (Phase 3 で実装予定)。
    #   公開 SaaS モードではこちらを設定し BILLING_SERVICE_URL と
    #   BILLING_API_KEY を併せて指定する。
    BILLING_BACKEND = os.environ.get("BILLING_BACKEND", "unlimited")
    BILLING_SERVICE_URL = os.environ.get("BILLING_SERVICE_URL", "")
    BILLING_API_KEY = os.environ.get("BILLING_API_KEY", "")

    # メール配信基盤の動作モード。
    # - "console" (default): 標準出力にダンプ。開発・テスト・セルフホストの
    #   既定値。実メール送信は発生しない。
    # - "smtp" / "ses" / "resend" 等: Phase 6 後続 PR で実装予定。
    # MAIL_FROM / MAIL_FROM_NAME は配信時の From ヘッダー組み立てに使う。
    MAIL_BACKEND = os.environ.get("MAIL_BACKEND", "console")
    MAIL_FROM = os.environ.get("MAIL_FROM", "noreply@example.com")
    MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "いいかんじ™家計簿")

    # 法的文書 (利用規約 / プライバシーポリシー / 特商法表記) で表示する
    # 運営者情報。実値はデプロイ時に環境変数で注入する想定で、ソース
    # 管理には含めない (Phase 1 #66 方針)。
    OPERATOR_NAME = os.environ.get("OPERATOR_NAME", "")
    OPERATOR_BUSINESS_FORM = os.environ.get("OPERATOR_BUSINESS_FORM", "個人事業主")
    OPERATOR_ADDRESS = os.environ.get("OPERATOR_ADDRESS", "")
    OPERATOR_PHONE = os.environ.get("OPERATOR_PHONE", "")
    OPERATOR_EMAIL = os.environ.get("OPERATOR_EMAIL", "")
    # 法的文書の最終更新日 (YYYY-MM-DD)。改訂時に手動更新する。
    OPERATOR_LEGAL_UPDATED_AT = os.environ.get("OPERATOR_LEGAL_UPDATED_AT", "")

    # 現在有効な利用規約・プライバシーポリシーのバージョン (YYYY-MM-DD 形式)。
    # 規約改訂時に環境変数で更新する。User.accepted_terms_version が
    # この値と一致しないユーザーは再同意フローに誘導される (Phase 1 #66)。
    # 未設定時は同意管理を実質オフ (= "" と扱う)。
    CURRENT_TERMS_VERSION = os.environ.get(
        "CURRENT_TERMS_VERSION", "2026-05-19"
    )

    # ローカル LLM (llama.cpp / llama-server) のエンドポイント。
    # サーバー管理者が用意する任意機能。未設定の場合、ユーザー UI で llama.cpp
    # プロバイダーは選択肢に出ず、既存 `llama_cpp` 設定を持つユーザーには
    # 「サーバー管理者が提供を停止しました」と案内される。
    LLAMA_CPP_URL = os.environ.get("LLAMA_CPP_URL", "")

    # 証憑画像ストレージ
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")  # "local" or "s3"
    STORAGE_LOCAL_DIR = os.environ.get("STORAGE_LOCAL_DIR", "/app/data/vouchers")
    STORAGE_S3_BUCKET = os.environ.get("STORAGE_S3_BUCKET", "")
    STORAGE_S3_ENDPOINT = os.environ.get("STORAGE_S3_ENDPOINT")
    STORAGE_S3_REGION = os.environ.get("STORAGE_S3_REGION")
    STORAGE_S3_ACCESS_KEY = os.environ.get("STORAGE_S3_ACCESS_KEY")
    STORAGE_S3_SECRET_KEY = os.environ.get("STORAGE_S3_SECRET_KEY")
