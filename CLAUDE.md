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
| settings | `/settings` | 設定トップ・外部AI・Passkey・月次確定・通知・APIキー管理・監査アクセス |
| webauthn | `/webauthn` | Passkey API（JSON、CSRF免除） |
| auditor | `/auditor` | 監査ダッシュボード・受信スナップショット閲覧（非同期ワークフロー） |
| vouchers | `/vouchers` | 証憑一覧（電帳法検索要件対応） |
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
| AuditGrant | audit_grants | owner_user_id, auditor_user_id, permission_level (1/2/3), revoked_at（status/submitted_at は代理閲覧撤去で廃止・DROP予定） |
| AuditPackage / AuditResponse | audit_packages / audit_responses | 非同期監査ワークフロー（owner→auditor の HPKE 暗号化スナップショット / auditor→owner の修正案） |
| AuditGrantAccount | audit_grant_accounts | audit_grant_id, account_user_id, account_code（Lv2の可視科目） |
| AIDraft | ai_drafts | user_id, status (pending/analyzed), image_path, suggestions (JSON), discord_webhook_url, discord_message_id |
| UserAIConfig | user_ai_configs | provider, api_key_encrypted, model_name, custom_prompt, base_url |
| AutoImportSource | auto_import_sources | user_id, source_type (webdav), config (JSON暗号化) |
| ProcessedFile | auto_import_processed_files | source_id, filename, draft_id |
| WebhookConfig | webhook_configs | user_id, url, events |
| Voucher | vouchers | user_id, journal_entry_id (SET NULL), image_key, image_mime, file_hash (SHA-256), uploaded_at |
| VoucherAuditLog | voucher_audit_logs | voucher_id, user_id, action (orphaned/hash_verified/hash_mismatch), detail (JSON) |
| BalanceCache | balance_caches | user_id, year, period, account_code, cumulative_debit, cumulative_credit |
| WebAuthnCredential | webauthn_credentials | credential_id, credential_public_key, current_sign_count |
| OAuthDevice | oauth_devices | device_code_hash, user_code, user_id, status (pending/approved/denied/expired/consumed), expires_at |
| OAuthToken | oauth_tokens | user_id, name, token_hash, token_prefix, is_active, last_used_at, revoked_at |

### サービス (`app/services/`)

| ファイル | 責務 |
|---------|------|
| accounting.py | 仕訳自動生成（出納帳→仕訳変換、振替、直接仕訳） |
| fiscal.py | 月次確定・年度オープン判定・期間チェック・元入金科目取得 |
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
- `proprietor`: 事業主 (3030) — 監査Lv2での科目隠蔽用

### 年度制限
- 前年以降: 常にオープン
- 前々年以前: FiscalClose レコードがなければ仕訳不可
- `is_year_open()`, `get_restricted_before_year()` で判定

### 月次確定
- FiscalClose.closed_period で確定済み月を管理（-1=未確定、0=期首のみ、1-12=月）
- 確定済み期間の仕訳は追加・変更・削除不可

### 監査用アカウント
- Lv1: 集計結果のみ / Lv2: 指定税務科目のみ / Lv3: 本人同等（`AuditGrant.permission_level`）
- **非同期スナップショットワークフロー**（owner が HPKE 暗号化したスナップショットを送信 → auditor が復号・監査 → 修正案を暗号化返信）。`AuditPackage` / `AuditResponse` で管理
- 旧リアルタイム代理閲覧（`acting_as_user_id` セッション方式）は撤去済み（#112）。移行手順は `docs/v5-e2ee/audit-migration.md`

### CSRF
- `CSRFProtect()` でグローバル有効
- メタタグ `<meta name="csrf-token">` で JS から取得可能
- WebAuthn API と REST API (`/api/v1`) は免除

## 電子帳簿保存法対応ロードマップ

AI証憑仕訳でアップロードされたレシート画像を電帳法（スキャナ保存）の要件に沿って保存するための計画。

### スマートフォン撮影の根拠

国税庁「電子帳簿保存法一問一答【スキャナ保存関係】」問4:
> 「スキャナ」とは書面の国税関係書類を電磁的記録に変換する入力装置をいい、
> スマートフォンやデジタルカメラ等についても該当すれば「スキャナ」に含まれる。

- H28年度改正で「原稿台と一体」の要件が撤廃され、スマホ撮影が正式に認められた
- 解像度 200dpi 相当（約387万画素）以上、RGB 各256階調以上 → 現行スマホは全て充足
- 2024年改正で解像度・階調・大きさの **情報の保存** 要件も撤廃（画像自体の品質要件は維持）
- 出典: https://www.nta.go.jp/law/joho-zeikaishaku/sonota/jirei/07scan/index.htm

### 入力期限（スキャナ保存）

- **早期入力方式**: 受領後おおむね **7営業日以内** にスキャン・保存
- **業務処理サイクル方式**: 事務処理規程を策定すれば最長 **2ヶ月+おおむね7営業日以内**
- 2024年改正: 訂正削除の履歴が残るシステムならタイムスタンプ不要

### 現状の問題

- v2.8.0 で画像を DB(LargeBinary) → ファイルシステム/S3 に移行済み
- **しかし仕訳登録後にドラフトごと画像を削除しており、証憑が残らない**
- 仕訳と証憑画像の紐付けがない

### Phase 1: 証憑の永続保存と仕訳紐付け

- `Voucher` モデル新設（user_id, journal_entry_id, image_key, image_mime, original_filename, uploaded_at, file_hash）
- 仕訳登録時: ドラフト削除、画像は `Voucher` に移行して永続保存
- `JournalEntry` → `Voucher` の 1:N リレーション（1仕訳に複数証憑）
- 元帳・仕訳詳細画面から証憑画像を閲覧可能に
- 仕訳削除時も証憑は保持（論理削除 or 孤立証憑として保存）

### Phase 1.5: AI コンプライアンスチェック

AI証憑仕訳の解析時に、電帳法スキャナ保存の要件を満たしているかをAIが自動チェック。設定画面でON/OFF可能。

- `UserAIConfig` に `compliance_check` (Boolean, default=False) を追加
- 有効時、画像解析プロンプト（Round 1）に電帳法チェック指示を注入:
  - **画像品質**: ピンぼけ・影・切れ・歪みの検出
  - **必須情報の視認性**: 日付・金額・取引先が読み取れるか
  - **入力期限**: レシート日付が古すぎないか（受領日から2ヶ月超の警告）
- AIレスポンスの `suggestions` に `compliance` フィールドを追加（pass/warn/fail + 理由）
- UI: 下書き一覧・レビュー画面にコンプライアンス結果を表示（警告バッジ等）
- 撮り直し推奨時はフラッシュメッセージで案内

### Phase 2: 検索要件（電帳法の検索機能要件）

電帳法スキャナ保存では「日付・金額・取引先」での検索が必須。

- 証憑一覧画面の新設（日付・金額・摘要で絞込）
- 仕訳の date / amount / description を経由した検索で要件を満たす
- 証憑画像のサムネイル表示

### Phase 3: 改ざん防止

電帳法では「訂正削除の事実と内容を確認できること」または「訂正削除ができないこと」が必要。

- 保存時に SHA-256 ハッシュを `Voucher.file_hash` に記録
- 証憑の上書き・削除を禁止（管理者操作のみ許可、操作ログ記録）
- S3 バックエンド使用時はオブジェクトバージョニングを推奨
- 個人事業主向けには「事務処理規程」方式（内部規程の整備）でも可

### Phase 4: タイムスタンプ（オプション）

- 認定タイムスタンプ局 (TSA) 連携は大規模向け。個人事業主はクラウド保存+訂正削除防止で代替可
- `Voucher.uploaded_at` を保存時刻の証跡として記録（Phase 1 で対応済み）
- 将来的に FreeTSA 等の外部 TSA 連携を検討

### 電帳法の主な要件チェックリスト

| 要件 | 状態 | 対応 Phase |
|------|------|-----------|
| 証憑の保存 | ✅ Voucher モデルで永続保存 | Phase 1 |
| 仕訳との相互関連性 | ✅ JournalEntry ↔ Voucher 1:N | Phase 1 |
| 見読可能性（表示・印刷） | ✅ 仕訳帳・元帳から画像閲覧可 | Phase 1 |
| AI コンプライアンスチェック | ✅ 設定画面でON/OFF可能 | Phase 1.5 |
| 検索機能（日付・金額・取引先） | ✅ 証憑一覧画面+API | Phase 2 |
| 訂正削除の防止/履歴 | ✅ VoucherAuditLog で操作ログ記録、ハッシュ検証 | Phase 3 |
| タイムスタンプ | ✅ uploaded_at 記録済み、TSA連携は将来検討 | Phase 1 / 4 |
| 入力期限の遵守（最長約2ヶ月7営業日） | ✅ 67日超過で警告バッジ表示 | Phase 4 |
| 解像度・階調の確保 | ✅ 原本画像をそのまま保存 | — |
| ストレージ抽象化 | ✅ local / S3 切替 | v2.8.0 済 |

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
