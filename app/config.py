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
    # 招待制ベータモード (Phase 8 #72)。"true" のとき register / register_auditor
    # は招待トークン (`?token=...`) が必須となり、メールアドレスと一致するもの
    # のみ受理する。`flask invite-create <email>` でトークンを発行できる。
    REGISTRATION_INVITE_ONLY = os.environ.get(
        "REGISTRATION_INVITE_ONLY", "false"
    ).lower() == "true"

    # E2EE 一斉移行 (§16) のメンテナンスウィンドウ実施日 (ISO 形式 "YYYY-MM-DD")。
    # `flask migration-lock-stale` がこの日から MIGRATION_LOCK_GRACE_DAYS (既定 30)
    # 経過しても鍵未設定のユーザーをロックする起点に使う。未設定 (空文字) なら
    # ロック CLI は no-op (セルフホスト・移行前・テスト用)。temp-MK 設定時刻を
    # ユーザー単位で保持しない設計のため、全体基準日で一斉判定する (E7 #114)。
    MIGRATION_WINDOW_DATE = os.environ.get("MIGRATION_WINDOW_DATE", "")
    # 鍵設定の猶予日数 (§16.5)。ロック猶予 (30) / 自動退会猶予 (ロック後 60)。
    MIGRATION_LOCK_GRACE_DAYS = int(
        os.environ.get("MIGRATION_LOCK_GRACE_DAYS", "30")
    )
    MIGRATION_PURGE_GRACE_DAYS = int(
        os.environ.get("MIGRATION_PURGE_GRACE_DAYS", "60")
    )
    # 運用者向け管理画面 (/admin/migration-progress, §16.6) の Basic 認証。
    # 現行の user_type (personal/auditor) では admin を表現できないため、
    # 暫定で環境変数の Basic 認証を採用 (設計書 §16.5 選択肢 c)。両方が設定
    # されている場合のみ /admin/* を有効化し、未設定なら 503 で機能無効。
    OPS_BASIC_AUTH_USER = os.environ.get("OPS_BASIC_AUTH_USER", "")
    OPS_BASIC_AUTH_PASS = os.environ.get("OPS_BASIC_AUTH_PASS", "")

    # エンタイトルメント基盤の動作モード。
    # - "unlimited" (default): セルフホスト前提で全有償機能を解放。
    #   billing コンテナへの HTTP リクエストは発生しない。フォーク
    #   自家用 / 内部利用 / 検証用途。
    # - "free_only": 全有償機能 (paid_llm / voucher_storage / audit_seat
    #   / timestamp_seal 等) を拒否し、無償ベース機能のみ提供する。
    #   billing コンテナを立てずに公開ベータを始めたい運用者向け。
    # - "http": billing コンテナに HTTP 照会 (Phase 3 で実装予定)。
    #   公開 SaaS 正式運用ではこちらを設定し BILLING_SERVICE_URL と
    #   BILLING_API_KEY を併せて指定する。
    BILLING_BACKEND = os.environ.get("BILLING_BACKEND", "unlimited")
    BILLING_SERVICE_URL = os.environ.get("BILLING_SERVICE_URL", "")
    BILLING_API_KEY = os.environ.get("BILLING_API_KEY", "")

    # メール配信基盤の動作モード。
    # - "console" (default): 標準出力にダンプ。開発・テスト・セルフホストの
    #   既定値。実メール送信は発生しない。
    # - "smtp": SMTP プロトコルで実送信 (公開 SaaS 運用向け)。`MAIL_SMTP_*`
    #   環境変数の設定が必須。
    # - "ses" / "resend" 等: 後続 PR で実装予定。
    # MAIL_FROM / MAIL_FROM_NAME は配信時の From ヘッダー組み立てに使う。
    MAIL_BACKEND = os.environ.get("MAIL_BACKEND", "console")
    MAIL_FROM = os.environ.get("MAIL_FROM", "noreply@example.com")
    MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "いいかんじ™家計簿")

    # MAIL_BACKEND=smtp のときに使用する SMTP 設定。
    # use_tls: "starttls" (587 推奨) / "ssl" (465) / "none" (平文、開発用のみ)
    MAIL_SMTP_HOST = os.environ.get("MAIL_SMTP_HOST", "")
    MAIL_SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT") or 587)
    MAIL_SMTP_USERNAME = os.environ.get("MAIL_SMTP_USERNAME", "")
    MAIL_SMTP_PASSWORD = os.environ.get("MAIL_SMTP_PASSWORD", "")
    MAIL_SMTP_USE_TLS = os.environ.get("MAIL_SMTP_USE_TLS", "starttls")
    MAIL_SMTP_TIMEOUT = int(os.environ.get("MAIL_SMTP_TIMEOUT") or 30)
    # お問い合わせ送信先 (運営者宛通知)。空文字なら通知メール送信なし。
    MAIL_CONTACT_TO = os.environ.get("MAIL_CONTACT_TO", "")

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
    #
    # デフォルトは空文字で、セルフホスト運用 (Phase 8) を意識して同意管理は
    # オフが既定。公開 SaaS としてデプロイする場合は環境変数で
    # 明示的にバージョンを設定する必要がある。
    CURRENT_TERMS_VERSION = os.environ.get("CURRENT_TERMS_VERSION", "")

    # 証憑画像ストレージ
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")  # "local" or "s3"
    STORAGE_LOCAL_DIR = os.environ.get("STORAGE_LOCAL_DIR", "/app/data/vouchers")
    STORAGE_S3_BUCKET = os.environ.get("STORAGE_S3_BUCKET", "")
    STORAGE_S3_ENDPOINT = os.environ.get("STORAGE_S3_ENDPOINT")
    STORAGE_S3_REGION = os.environ.get("STORAGE_S3_REGION")
    STORAGE_S3_ACCESS_KEY = os.environ.get("STORAGE_S3_ACCESS_KEY")
    STORAGE_S3_SECRET_KEY = os.environ.get("STORAGE_S3_SECRET_KEY")

    # 証憑画像のストレージクオータ (バイト)。`voucher_storage` 有償プラン
    # 契約者の上限値。Phase 5 #70 のデフォルト 500 MB。セルフホスト運用
    # では `UnlimitedBillingClient` で entitlement が常に True を返すため
    # この上限のみが効く。
    STORAGE_QUOTA_BYTES_DEFAULT = int(
        os.environ.get("STORAGE_QUOTA_BYTES_DEFAULT", str(500 * 1024 * 1024))
    )

    # E6 #113 §15.4 PR-2: サーバ保存版データエクスポート。
    # EXPORT_TTL_HOURS = DL リンクの有効期間 (期限切れは 410 + PR-3 で物理削除)。
    # EXPORT_MAX_DOWNLOADS = 1 ジョブの最大 DL 回数 (超過で 410)。
    # EXPORT_MAX_UPLOAD_BYTES = アップロード暗号化 zip の上限 (DoS 防止)。
    EXPORT_TTL_HOURS = int(os.environ.get("EXPORT_TTL_HOURS", "24"))
    EXPORT_MAX_DOWNLOADS = int(os.environ.get("EXPORT_MAX_DOWNLOADS", "3"))
    EXPORT_MAX_UPLOAD_BYTES = int(
        os.environ.get("EXPORT_MAX_UPLOAD_BYTES", str(500 * 1024 * 1024))
    )
