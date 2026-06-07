# いいかんじ™家計簿 - Claude Code ガイド

## プロジェクト概要

個人向け複式簿記家計簿アプリ。Flask + PostgreSQL + Bootstrap 5。
出納帳入力で仕訳を自動生成し、CSV/OFX/Web貼り付けで銀行明細を一括取込。AIによる証憑仕訳やPasskey認証にも対応。

## ブランチ運用

- `develop`: 開発ブランチ（GitHubデフォルト）
- `master`: リリースブランチ（タグ付きリリースのみ）

## ドキュメント

- リリースノート: `docs/releases.md`

## リリース手順

1. `develop` で開発・コミット・プッシュ
2. **`docker-compose.yml.example` の `image:` バージョンを新タグに更新してコミット**（master マージ前に行うこと）
3. `git checkout master && git merge develop`
4. `git tag -a vX.Y.Z -m "メッセージ"` → `git push origin master vX.Y.Z`
5. `git checkout develop`
6. GitHub Actions (`.github/workflows/build-and-push.yml`) が GHCR にイメージをビルド・プッシュ
7. サーバーへのデプロイ・リスタートはユーザーが手動で行う
8. **Python クライアント (`../client-py`) の仕様見直し・リリースも行うこと** — API のリクエスト/レスポンス形式やエンドポイントに変更がある場合、クライアント側のコード・ドキュメントを更新し、クライアントも合わせてリリースする

## 技術スタック

- Python 3.12 / Flask 3.x / Gunicorn
- PostgreSQL 16 / SQLAlchemy 2.x / Alembic (Flask-Migrate)
- Flask-Login / Flask-WTF (CSRF) / py_webauthn
- Bootstrap 5.3 (CDN) / Bootstrap Icons / Chart.js
- Docker Compose (Tailscale + Web + DB)

## アプリ構造

### App Factory (`app/__init__.py`)
- `create_app()` で Flask アプリを生成
- 16個の Blueprint を登録
- WebAuthn API は CSRF を免除 (`csrf.exempt`)
- before_request フックで顧問権限を制御
- テンプレートフィルタ `mask_account` で顧問アクセス時の科目隠蔽
- CLI: `flask seed`（科目区分投入）、`flask seed-user`（ユーザー別科目投入）

### Blueprint 一覧

| Blueprint | URL prefix | 用途 |
|-----------|-----------|------|
| auth | `/` | 認証（個人/顧問ログイン・登録） |
| dashboard | `/` | ダッシュボード |
| cashbook | `/cashbook` | 出納帳 |
| journal | `/journal` | 仕訳伝票・一括削除・科目推定API |
| medical | `/medical` | 医療費管理 |
| reports | `/reports` | 試算表・元帳・損益計算書・貸借対照表・月次比較・確定申告集計 |
| accounts | `/accounts` | 勘定科目管理（JSON API） |
| csv_import | `/csv-import` | CSV取込 |
| ofx_import | `/ofx-import` | OFX取込 |
| web_import | `/web-import` | Web貼り付け取込 |
| ai_journal | `/ai-journal` | AI証憑仕訳 |
| settings | `/settings` | 設定トップ・外部AI・Passkey・月次確定・通知・APIキー管理・顧問アクセス |
| webauthn | `/webauthn` | Passkey API（JSON、CSRF免除） |
| auditor | `/auditor` | 顧問ダッシュボード・代理閲覧 |
| vouchers | `/vouchers` | 証憑一覧（日付・金額・取引先で検索） |
| api | `/api/v1` | REST API（仕訳CRUD・AI証憑仕訳・Bearer認証） |
| oauth | `/oauth` | OAuth 2.0 Device Authorization Grant (RFC 8628) |

### モデル (`app/models/`)

| モデル | テーブル | 主なカラム |
|--------|---------|-----------|
| User | users | username, email, password_hash, user_type (personal/auditor) |
| AccountType | account_types | name, code (asset/liability/equity/revenue/expense), normal_balance |
| Account | accounts | PK(user_id, code), name, tax_category, cost_type, system_role, is_active, deactivated_year |
| JournalEntry | journal_entries | user_id, date, entry_number, description, source, batch_id, fiscal_period |
| JournalEntryLine | journal_entry_lines | journal_entry_id, account_user_id, account_code, debit_amount, credit_amount |
| FiscalClose | fiscal_closes | user_id, year, closed_period |
| MedicalExpense | medical_expenses | patient_name, hospital_name, amount_paid, insurance_reimbursement, provider_type |
| AuditGrant | audit_grants | owner_user_id, auditor_user_id, permission_level (1/2/3), status |
| AuditGrantAccount | audit_grant_accounts | audit_grant_id, account_user_id, account_code（Lv2の可視科目） |
| AIDraft | ai_drafts | user_id, status (pending/analyzed), image_path, suggestions (JSON), discord_webhook_url, discord_message_id |
| UserAIConfig | user_ai_configs | provider, api_key_encrypted, model_name, custom_prompt, base_url |
| AutoImportSource | auto_import_sources | user_id, source_type (webdav), config (JSON暗号化) |
| ProcessedFile | auto_import_processed_files | source_id, filename, draft_id |
| WebhookConfig | webhook_configs | user_id, url, events |
| Voucher | vouchers | user_id, journal_entry_id (SET NULL), image_key, image_mime, file_hash (SHA-256), uploaded_at |
| BalanceCache | balance_caches | user_id, year, period, account_code, cumulative_debit, cumulative_credit |
| WebAuthnCredential | webauthn_credentials | credential_id, credential_public_key, current_sign_count |
| OAuthDevice | oauth_devices | device_code_hash, user_code, user_id, status (pending/approved/denied/expired/consumed), expires_at |
| OAuthToken | oauth_tokens | user_id, name, token_hash, token_prefix, is_active, last_used_at, revoked_at |

### サービス (`app/services/`)

| ファイル | 責務 |
|---------|------|
| accounting.py | 仕訳自動生成（出納帳→仕訳変換、振替、直接仕訳） |
| fiscal.py | 月次確定・年度オープン判定・期間チェック・元入金科目取得 |
| audit.py | 顧問権限・代理閲覧・科目隠蔽・提出ロック |
| csv_import.py | CSVパース（エンコーディング自動判定・日付/金額パース） |
| ofx_import.py | OFX/QFXパース |
| ai_receipt.py | AI証憑解析・Web明細抽出（OpenAI/Gemini/Claude/Ollama対応） |
| tax.py | 確定申告集計・月次比較・着地予測 |
| balance_cache.py | 確定済み残高キャッシュの保存・取得 |
| captcha.py | CAPTCHA 検証（hCaptcha/reCAPTCHA/Turnstile/mCaptcha） |
| notify.py | Webhook 通知（Discord等） |
| auto_import.py | 自動取込オーケストレーター（内部利用、UIはオプトアウト済み） |
| voucher.py | ドラフト→証憑移行ヘルパー (create_voucher_from_draft) |
| storage.py | 証憑画像ストレージ抽象化（Local/S3）・サムネイル生成 |
| image.py | 画像配信ヘルパー（キャッシュ・ETag/304・send_file・S3 presigned URL） |
| seed.py | 標準科目の初期データ・system_role定義 |

### JS (`app/static/js/`)

| ファイル | 用途 |
|---------|------|
| app.js | 数値入力のEnterキー制御、証憑プレビュー共通関数 |
| alpine-components.js | Alpine.js 共通コンポーネント定義（段階的に拡充） |
| drag_select.js | ドラッグ選択（バックトラック取消対応） |
| webauthn.js | Passkey 登録・認証 |

### フロントエンドフレームワーク

- **htmx 2.0** — サーバー通信の宣言的HTML属性化（CDN）
- **Alpine.js 3.x** — クライアント状態管理のリアクティブ化（CDN, defer）
- Bootstrap 5 はスタイリング・モーダル・タブに引き続き使用
- ビルドシステムなし（CDN のみ）
- htmx の CSRF トークンは `htmx:configRequest` イベントで自動付与
- htmx → Toast 連携: `HX-Trigger: showToast` ヘッダで `showToast()` を呼び出し

### Alpine.js コンポーネント (`alpine-components.js`)

| コンポーネント | 用途 |
|-------------|------|
| `fiscalPeriodChecker` | 月次確定・未開設年度チェック（仕訳/出納帳/AI仕訳で共用） |
| `bulkSelect` | 仕訳帳一覧の一括選択バー |
| `accountEditor` | 勘定科目管理の CRUD |
| `accountSelector` | 科目選択モーダル（グローバル `openAccountSelector()` で呼出） |
| `journalLines` | 仕訳明細行の動的追加・削除・合計計算 |
| `importConfirm` | 取込確認画面（CSV/OFX/Web共通） |

### フロントエンド実装パターン

- **Alpine コンポーネント**: `Alpine.data('name', function(config) { ... })` で定義。非リアクティブな閉包変数は外側 `var` で宣言
- **Alpine スコープ入れ子**: 子の `x-data` から親スコープのプロパティにアクセス可能（例: `fiscalPeriodChecker` → `journalLines` の `lines`）
- **フォーム送信**: `@submit="serializeLines()"` + `this.$refs.xxx.value = JSON.stringify(data)` で hidden input に書込み（Alpine の非同期 DOM 更新を回避）
- **科目選択モーダル**: `openAccountSelector(callback, filter)` → `CustomEvent('account-selected')` で Alpine に通知
- **htmx 削除**: `hx-post` + `hx-confirm` + `hx-target="closest tr"` + `hx-swap="outerHTML swap:0.3s"`. バックエンドは `HX-Request` 検出で空レスポンス + `HX-Trigger` 返却
- **drag_select + Alpine**: `CustomEvent('drag-select-update')` → `@drag-select-update="handler()"` で連携
- **テンプレート共通化**: `{% set var %}...{% endset %}` + `{% include "partial" %}` パターン

## 重要な設計パターン

### 複式簿記
- 全取引は debit_amount + credit_amount が一致する仕訳として記録
- fiscal_period: 0=期首, 1-12=月, 13-15=決算整理, 16=損益振替（自動生成専用、手動入力不可）

### 仕訳の source
- `journal`: 仕訳帳から直接入力
- `cashbook`: 出納帳から自動生成
- `ai_receipt`: AI証憑仕訳
- `csv` / `ofx` / `web`: 各種取込
- batch_id (UUID) で一括取込の単位を管理

### 勘定科目の system_role
特殊な科目は `system_role` カラムで識別:
- `capital`: 元入金 (3010)
- `retained_earnings`: 繰越利益 (3020)
- `proprietor`: 事業主 (3030) — 顧問Lv2での科目隠蔽用

### 年度制限
- 前年以降: 常にオープン
- 前々年以前: FiscalClose レコードがなければ仕訳不可
- `is_year_open()`, `get_restricted_before_year()` で判定

### 月次確定
- FiscalClose.closed_period で確定済み月を管理（-1=未確定、0=期首のみ、1-12=月）
- 確定済み期間の仕訳は追加・変更・削除不可

### 顧問用アカウント
- Lv1: 集計結果のみ閲覧
- Lv2: 指定された税務科目の閲覧・編集、非公開科目は「事業主」で隠蔽
- Lv3: 本人同等の全操作
- セッションに `acting_as_user_id` で代理閲覧状態を管理

### CSRF
- `CSRFProtect()` でグローバル有効
- メタタグ `<meta name="csrf-token">` で JS から取得可能
- WebAuthn API と REST API (`/api/v1`) は免除

## コーディング規約・注意事項

### マイグレーション
- revision ID は `NNN_snake_case` 形式（例: `010_system_role`）
- `down_revision` は前のマイグレーションの `revision` 値と**完全一致**させること
- 起動時に `flask db upgrade` が自動実行される (entrypoint.sh)

### テンプレート
- HTML の `<form>` はネストできない。外側に `<form>` がある場合、個別のPOSTは JS で動的に form を生成して submit する
- 取込確認画面（CSV/OFX/Web）は `_partials/import_confirm_table.html` + Alpine `importConfirm` コンポーネントで共通化。テンプレートの構造を変える場合は **パーシャルと alpine-components.js の両方を揃える**こと
- `_partials/account_selector.html` は科目選択モーダルの共通パーツ

### テスト
- `tests/conftest.py` に SQLite in-memory のフィクスチャあり
- pytest で実行: `docker exec -w /app server-web-1 python -m pytest tests/ -v`
- GitHub Actions (`.github/workflows/test.yml`) で push/PR 時に自動実行
- 445テスト: accounting(16), api(27), audit(43), balance_cache(15), compliance(14), csv_import(53), fiscal(26), models(12), monthly_report(8), ofx_import(15), settings(7), storage(25), tax(45), security_auth(27), security_idor(12), security_input(12), security_csrf(9), security_headers(7), security_ratelimit(3), voucher(23), voucher_search(14), voucher_tamper(15), timestamp(12)
- E2Eテスト (Playwright/Firefox): `npx playwright test tests/e2e/` — 設定画面の表示・遷移(10), ドラッグ選択(15)。CIでも自動実行
- 税務集計・プライバシー権限は重点テスト項目

### Docker
- `entrypoint.sh`: `flask db upgrade` → `flask seed` → `flask run`
- 本番は Tailscale 経由でアクセス

### GitHub Actions
- `.github/workflows/test.yml`: push/PR 時に pytest + Playwright E2E 自動実行
- `.github/workflows/build-and-push.yml`: GHCR へ Docker イメージをビルド・プッシュ
- `.github/workflows/pages.yml`: docs/ 配下変更時に GitHub Pages をデプロイ
