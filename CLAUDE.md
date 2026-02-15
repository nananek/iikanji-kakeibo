# いいかんじ™家計簿 - Claude Code ガイド

## プロジェクト概要

個人向け複式簿記家計簿アプリ。Flask + PostgreSQL + Bootstrap 5。
出納帳入力で仕訳を自動生成し、CSV/OFX/Web貼り付けで銀行明細を一括取込。AIによる証憑仕訳やPasskey認証にも対応。

## ブランチ運用

- `develop`: 開発ブランチ（GitHubデフォルト）
- `master`: リリースブランチ（タグ付きリリースのみ）

## リリース手順

1. `develop` で開発・コミット・プッシュ
2. `git checkout master && git merge develop`
3. `git tag -a vX.Y.Z -m "メッセージ"` → `git push origin master vX.Y.Z`
4. `git checkout develop`
5. **`docker-compose.yml.example` の `image:` バージョンを新タグに更新すること**（忘れがち）
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
- 14個の Blueprint を登録
- WebAuthn API は CSRF を免除 (`csrf.exempt`)
- before_request フックで監査権限を制御
- テンプレートフィルタ `mask_account` で監査時の科目隠蔽
- CLI: `flask seed`（科目区分投入）、`flask seed-user`（ユーザー別科目投入）

### Blueprint 一覧

| Blueprint | URL prefix | 用途 |
|-----------|-----------|------|
| auth | `/` | 認証（個人/監査ログイン・登録） |
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
| settings | `/settings` | 設定（AI API・Passkey・月次確定・年度管理・監査アクセス） |
| webauthn | `/webauthn` | Passkey API（JSON、CSRF免除） |
| auditor | `/auditor` | 監査ダッシュボード・代理閲覧 |

### モデル (`app/models/`)

| モデル | テーブル | 主なカラム |
|--------|---------|-----------|
| User | users | username, email, password_hash, user_type (personal/auditor) |
| AccountType | account_types | name, code (asset/liability/equity/revenue/expense), normal_balance |
| Account | accounts | user_id, code, name, tax_category, cost_type, system_role, is_active, deactivated_year |
| JournalEntry | journal_entries | user_id, date, entry_number, description, source, batch_id, fiscal_period |
| JournalEntryLine | journal_entry_lines | journal_entry_id, account_id, debit_amount, credit_amount |
| FiscalClose | fiscal_closes | user_id, year, closed_period |
| MedicalExpense | medical_expenses | patient_name, hospital_name, amount_paid, insurance_reimbursement, provider_type |
| AuditGrant | audit_grants | owner_user_id, auditor_user_id, permission_level (1/2/3), status |
| AuditGrantAccount | audit_grant_accounts | audit_grant_id, account_id（Lv2の可視科目） |
| UserAIConfig | user_ai_configs | provider, api_key_encrypted, model_name |
| WebAuthnCredential | webauthn_credentials | credential_id, credential_public_key, current_sign_count |

### サービス (`app/services/`)

| ファイル | 責務 |
|---------|------|
| accounting.py | 仕訳自動生成（出納帳→仕訳変換、振替、直接仕訳） |
| fiscal.py | 月次確定・年度オープン判定・期間チェック・元入金科目取得 |
| audit.py | 監査権限・代理閲覧・科目隠蔽・提出ロック |
| csv_import.py | CSVパース（エンコーディング自動判定・日付/金額パース） |
| ofx_import.py | OFX/QFXパース |
| ai_receipt.py | AI証憑解析・Web明細抽出（OpenAI/Gemini/Claude対応） |
| tax.py | 確定申告集計・月次比較・着地予測 |
| seed.py | 標準科目の初期データ・system_role定義 |

### JS (`app/static/js/`)

| ファイル | 用途 |
|---------|------|
| app.js | PWA Service Worker 登録、数値入力のEnterキー制御 |
| import_confirm.js | **取込確認画面の共通ロジック**（CSV/OFX/Web共通） |
| drag_select.js | ドラッグ選択（バックトラック取消対応） |
| webauthn.js | Passkey 登録・認証 |

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
- `proprietor`: 事業主 (3030) — 監査Lv2での科目隠蔽用

### 年度制限
- 前年以降: 常にオープン
- 前々年以前: FiscalClose レコードがなければ仕訳不可
- `is_year_open()`, `get_restricted_before_year()` で判定

### 月次確定
- FiscalClose.closed_period で確定済み月を管理（-1=未確定、0=期首のみ、1-12=月）
- 確定済み期間の仕訳は追加・変更・削除不可

### 監査用アカウント
- Lv1: 集計結果のみ閲覧
- Lv2: 指定された税務科目の閲覧・編集、非公開科目は「事業主」で隠蔽
- Lv3: 本人同等の全操作
- セッションに `acting_as_user_id` で代理閲覧状態を管理

### CSRF
- `CSRFProtect()` でグローバル有効
- メタタグ `<meta name="csrf-token">` で JS から取得可能
- WebAuthn API のみ免除

## コーディング規約・注意事項

### マイグレーション
- revision ID は `NNN_snake_case` 形式（例: `010_system_role`）
- `down_revision` は前のマイグレーションの `revision` 値と**完全一致**させること
- 起動時に `flask db upgrade` が自動実行される (entrypoint.sh)

### テンプレート
- HTML の `<form>` はネストできない。外側に `<form>` がある場合、個別のPOSTは JS で動的に form を生成して submit する
- 取込確認画面（CSV/OFX/Web）は `import_confirm.js` で共通化。テンプレートの構造を変える場合は **3つとも揃える**こと
- `_partials/account_selector.html` は科目選択モーダルの共通パーツ

### テスト
- `tests/conftest.py` に SQLite in-memory のフィクスチャあり
- テストカバレッジは限定的

### Docker
- `entrypoint.sh`: `flask db upgrade` → `flask seed` → `flask run`
- 本番は Tailscale 経由でアクセス
