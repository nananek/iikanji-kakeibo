<div align="center">

# いいかんじ™家計簿

### 家計簿なのに、ちゃんと複式簿記。

**出納帳に入力するだけで、裏で仕訳が自動生成。**
**CSV・OFX・Webページ貼り付けで銀行明細を一括取込。**
**AIが証憑を読み取って仕訳を提案。**
**確定申告に必要な控除額も自動集計。**
**税理士・公認会計士との連携も、監査用アカウントでいいかんじに。**

---

</div>

- **かんたん** — 簿記を知らなくても使える出納帳UI
- **ほんかく** — 複式簿記の仕訳帳・元帳・試算表を完備
- **べんり** — CSV / OFX / Web貼り付け / AI証憑仕訳で入力を省力化
- **かしこい** — 固定費・変動費・随時費の区分で月末着地を自動予測
- **つながる** — 3段階の権限レベルで税理士・公認会計士と安全にデータ共有

<div align="center">

---

</div>


## 主な機能

### AI 証憑仕訳

- 領収書・給与明細・請求書の画像をアップロードするだけで、AI が仕訳案を複数提案
- OpenAI / Google Gemini / Anthropic Claude / Ollama (ローカル) に対応
- コメントで「会社の懇親会」等のヒントを付けると精度向上
- 定型プロンプトで決済手段や摘要ルールを毎回自動指示
- 下書き一覧から「案1で登録」ボタンでワンタップ仕訳登録
- 仕訳登録後は下書き自動削除、Discord Webhook に完了マーク
- 証憑画像はファイルシステムまたは S3 互換ストレージに永続保存（環境変数で切替）
- 電帳法コンプライアンスチェック（画像品質・必須情報・入力期限をAIが自動判定）

### 電帳法スキャナ保存対応

電子帳簿保存法（スキャナ保存）の要件に対応:

- **証憑の永続保存** — AI証憑仕訳で登録した画像を `Voucher` として永続保存し、仕訳と 1:N で紐付け
- **検索機能** — 日付・金額・取引先（摘要）で証憑を検索できる一覧画面を提供
- **改ざん防止** — 保存時に SHA-256 ハッシュを記録し、いつでも検証可能。操作ログで改ざんを検出
- **入力期限チェック** — レシート日付から約2ヶ月+7日（67日）を超過した証憑に警告バッジを表示
- **見読可能性** — 仕訳帳・元帳・証憑一覧から画像を閲覧可能
- 仕訳削除時も証憑は保持（孤立証憑として操作ログに記録）

### 明細取込 (CSV / OFX / Web)

- **CSV** — 列マッピング UI で日付・摘要・入金・出金を指定。Shift-JIS / UTF-8 自動判定
- **OFX** — 銀行の OFX/QFX ファイルをそのまま読込
- **Web** — テキスト貼り付けで AI が明細を自動抽出。ブックマークレット「明細ピッカー」対応
- 科目自動推定（履歴ベース + AI 一括推定）、ドラッグ選択で一括科目設定
- 日付編集・全カラムソート・年度制限チェック・sticky ツールバー

### 出納帳 / 仕訳伝票

- **出納帳** — 日付・口座・費目・金額・摘要を入力するだけで仕訳を自動生成
- **仕訳伝票** — 借方・貸方を自由に入力。行の動的追加と貸借バランスのリアルタイムチェック

### レポート

- ダッシュボード（月次/年次の収支サマリー、月別推移グラフ）
- 残高試算表（期間バードラッグ選択、貸借一致チェック）
- 総勘定元帳（月区切り・期間絞込・モーダル編集・削除）
- 損益計算書・貸借対照表
- 月次比較（12ヶ月推移、固定/変動/随時の区分分析、着地予測）
- 確定申告用集計（社会保険・生命保険・寄附金・iDeCo・源泉所得税、医療費控除の受診者別集計と CSV エクスポート）

### 医療費管理

- 受診者・病院名・治療内容・支払額・保険補填額を記録
- 年間の自己負担額を自動集計し、確定申告の医療費控除に対応

### 月次確定・年度管理

- 月ごとに帳簿を「確定」して編集をロック。確定済み残高をキャッシュし試算表・元帳を高速化
- 年末に損益振替仕訳を自動生成（専用の「振替」期間）
- 前々年以前は明示的に「開設」しない限り仕訳不可。年度間の整合性を自動保護
- インポート時に古い年度の取引は無視 or 元入金仕訳に変換を選択可能

### 監査用アカウント

税理士・公認会計士向けに3段階の権限レベルでデータ共有:
- **Lv1** 集計結果のみ閲覧
- **Lv2** 指定された税務科目の閲覧・編集（非公開科目は「事業主」で隠蔽）
- **Lv3** 本人同等の全操作
- 提出ロック・部分編集・科目隠蔽・ログイン分離

### REST API

- 仕訳の起票・閲覧・削除、AI 証憑仕訳、証憑管理を外部プログラムから操作
- 証憑一覧・画像取得・ハッシュ検証・操作ログの API を提供
- レポート API: 試算表・損益計算書・月次比較・確定申告集計を JSON で取得
- 認証は **API キー** または **OAuth 2.0 Device Flow** (RFC 8628) の Bearer トークン
- OAuth トークンは「全権限」または **「読み取り専用」** で承認可能。読取専用トークンは仕訳作成・削除・AI 解析が構造的に拒否される
- Python クライアント [`iikanji`](https://github.com/nananek/iikanji-kakeibo-client-py) (CLI/SDK) と TUI クライアント [`iikanji-tui`](https://github.com/nananek/iikanji-kakeibo-client-tui) を提供
- **MCP サーバー** [`iikanji-mcp`](https://github.com/nananek/iikanji-kakeibo-client-mcp) — Claude Desktop 等の MCP クライアントから財務分析を実行

### 公開運用 (SaaS) 向け機能

セルフホスト運用と並行して、不特定多数を対象とした公開運用にも対応:

- **法的文書ページ** — 利用規約 / プライバシーポリシー / 特定商取引法に基づく表記 (`/legal/terms`, `/legal/privacy`, `/legal/tokushoho`)。運営者情報は `OPERATOR_*` 環境変数で注入
- **利用規約への同意フロー** — 規約改訂時 (`CURRENT_TERMS_VERSION`) に既存ユーザーへ強制再同意を求める
- **お問い合わせフォーム** — `/legal/contact` で運営者・送信者双方にメール通知 (CAPTCHA + レート制限)
- **退会フロー** — `/settings/delete-account` で全データ削除 (Passkey 専用ユーザー対応、GDPR 消去権)。電帳法証跡のみ匿名化保持
- **招待制ベータモード** — `REGISTRATION_INVITE_ONLY=true` で `flask invite-create <email>` で発行した招待トークン経由のみ登録可能
- **ストレージクオータ** — 証憑画像の容量を `voucher_storage` エンタイトルメントで制御。80%/95% 到達時にメール通知
- **メール配信** — SMTP バックエンドを内蔵 (`MAIL_BACKEND=smtp`、STARTTLS / SSL 対応)。通知テンプレートは `templates/email/`

### その他

- **Passkey (WebAuthn)** — 指紋認証・Face ID でログイン
- **PWA** — ホーム画面に追加してネイティブアプリのように利用。セッション永続化
- **モバイル最適化** — 列の自動非表示・タッチ操作対応・期間バー縮小
- **勘定科目管理** — 標準約40科目を自動投入（確定申告用税区分付き）。独自科目の追加・編集・無効化が可能

<details>
<summary>標準勘定科目一覧</summary>

| 区分 | コード | 科目名 | 税区分 |
|---|---|---|---|
| 資産 | 1010 | 現金 | |
| | 1020 | 普通預金 | |
| | 1030 | 定期預金 | |
| | 1040 | 電子マネー | |
| | 1050 | 有価証券 | |
| | 1060 | 未収入金 | |
| 負債 | 2010 | クレジットカード | |
| | 2020 | 未払金 | |
| | 2030 | 借入金 | |
| | 2040 | 住宅ローン | |
| 純資産 | 3010 | 元入金 | |
| | 3020 | 繰越利益 | |
| | 3030 | 事業主 | (監査用) |
| 収益 | 4010 | 給与収入 | |
| | 4020 | 事業収入 | |
| | 4030 | 利息収入 | |
| | 4040 | 配当収入 | |
| | 4050 | 雑収入 | |
| 費用 | 5010〜5120 | 食費 / 住居費 / 水道光熱費 / 通信費 / 交通費 / 日用品費 / 被服費 / 美容費 / 交際費 / 趣味・娯楽費 / 教育費 / 雑費 | |
| | 6010 | 医療費 | 医療費控除 |
| | 6020〜6070 | 健康保険料 / 厚生年金保険料 / 国民年金保険料 / 国民健康保険料 / 雇用保険料 / 介護保険料 | 社会保険料控除 |
| | 7010〜7020 | 生命保険料 / 個人年金保険料 | 生命保険料控除 |
| | 7030 | 地震保険料 | 地震保険料控除 |
| | 7040〜7050 | ふるさと納税 / その他寄附金 | 寄附金控除 |
| | 7060〜7070 | iDeCo掛金 / 小規模企業共済 | 小規模企業共済等掛金控除 |
| | 8010 | 源泉所得税 | 源泉所得税 |
| | 8020 | 住民税 | 住民税 |

</details>

## 技術スタック

- Python 3.12 / Flask 3.x / Gunicorn
- PostgreSQL 16
- SQLAlchemy 2.x / Alembic (Flask-Migrate)
- Flask-Login / Flask-WTF / Flask-Limiter
- Pillow (サムネイル生成)
- py_webauthn (Passkey / WebAuthn)
- htmx 2.0 / Alpine.js 3.x (CDN) — 宣言的 AJAX・リアクティブ UI
- Bootstrap 5.3 (CDN)
- Docker Compose

## セットアップ

```bash
# リポジトリをクローン
git clone https://github.com/nananek/iikanji-kakeibo.git
cd iikanji-kakeibo

# Docker Compose 設定をコピー
#   開発用（ポート 5000 直接公開）:
cp docker-compose.dev.yml.example docker-compose.yml
#   本番用（Tailscale 経由でアクセス）:
#   cp docker-compose.yml.example docker-compose.yml

# .env を生成（SECRET_KEY・POSTGRES_PASSWORD をランダム値で初期化）
./setup-env.sh
# WEBAUTHN_RP_ID / WEBAUTHN_ORIGIN は環境に合わせて .env を編集してください

# 起動（初回はマイグレーションと勘定科目区分の投入を自動実行）
docker compose up -d
```

ブラウザで http://localhost:5000 を開き、ユーザー登録してください。登録時に標準勘定科目が自動で投入されます。

> **Note:** CAPTCHA を有効にする場合は `.env` に `CAPTCHA_PROVIDER` / `CAPTCHA_SITE_KEY` / `CAPTCHA_SECRET_KEY` を設定してください。詳細は `.env.example` のコメントを参照。

### 公開運用に必要な環境変数

不特定多数を対象に SaaS として運用する場合、以下も設定してください:

```bash
# 法的文書 (運営者情報) — /legal/* で表示される
OPERATOR_NAME="運営者名"
OPERATOR_BUSINESS_FORM="個人事業主"   # or "法人"
OPERATOR_ADDRESS="〒XXX-XXXX ..."
OPERATOR_PHONE="03-XXXX-XXXX"
OPERATOR_EMAIL="contact@example.com"
OPERATOR_LEGAL_UPDATED_AT="2026-05-19"

# 利用規約バージョン (改訂時に変更すると既存ユーザーへ再同意フロー誘導)
CURRENT_TERMS_VERSION="2026-05-19"

# 招待制ベータモード (推奨: 初期公開時)
REGISTRATION_INVITE_ONLY=true

# エンタイトルメント (有償機能) のモード。**運用形態によって設定を変える**:
#   - セルフホスト / 個人利用: unlimited (ENV 未設定でも同値、全機能解放)
#   - 公開ベータ (billing コンテナなし): free_only (全有償機能を拒否)
#   - 公開 SaaS 正式運用 (Phase 3 で実装): http (billing コンテナに HTTP 照会)
#
#   unlimited (default) — セルフホスト前提で全機能解放
#   free_only           — 全有償機能を拒否、無償ベース機能のみ提供
#                         (billing コンテナを立てずに公開ベータを始める運用者向け)
#   http                — billing コンテナに HTTP 照会 (Phase 3 で実装予定)
BILLING_BACKEND=free_only

# メール送信 (公開時はほぼ必須)
MAIL_BACKEND=smtp
MAIL_FROM="noreply@your-domain.com"
MAIL_FROM_NAME="いいかんじ™家計簿"
MAIL_SMTP_HOST="smtp.your-provider.com"
MAIL_SMTP_PORT=587
MAIL_SMTP_USERNAME="..."
MAIL_SMTP_PASSWORD="..."
MAIL_SMTP_USE_TLS=starttls  # starttls / ssl / none
MAIL_CONTACT_TO="contact@your-domain.com"  # お問い合わせフォーム宛先

# URL 生成 (絶対 URL のため必須)
SERVER_NAME="your-domain.com"
PREFERRED_URL_SCHEME=https
```

招待トークンの発行:
```bash
docker exec -w /app server-web-1 flask invite-create user@example.com
# → メール送信 + raw トークンは DB に保存されない
```

運用 (バックアップ・監視) の詳細は [docs/operations/](https://nananek.github.io/iikanji-kakeibo/operations/) を参照。

## プロジェクト構成

```
app/
├── __init__.py          # Flask app factory
├── config.py            # 設定
├── extensions.py        # db, migrate, login_manager, limiter
├── models/              # SQLAlchemy モデル
│   ├── user.py          #   User (user_type: personal/auditor)
│   ├── account.py       #   AccountType, Account
│   ├── journal.py       #   JournalEntry, JournalEntryLine
│   ├── fiscal.py        #   FiscalClose (月次確定・年度開設)
│   ├── balance_cache.py #   BalanceCache (確定済み残高キャッシュ)
│   ├── medical.py       #   MedicalExpense
│   ├── audit.py         #   AuditGrant, AuditGrantAccount
│   ├── api_key.py       #   APIKey (REST API 認証)
│   ├── ai_config.py     #   UserAIConfig (定型プロンプト・base_url含む)
│   ├── auto_import.py   #   AutoImportSource, ProcessedFile, WebhookConfig
│   ├── voucher.py       #   Voucher (証憑画像・仕訳紐付け・SHA-256ハッシュ)
│   ├── voucher_audit_log.py # VoucherAuditLog (改ざん防止ログ)
│   └── webauthn.py      #   WebAuthnCredential
├── views/               # Blueprint (ルーティング)
│   ├── auth.py          #   認証 (個人/監査用ログイン・登録)
│   ├── dashboard.py     #   ダッシュボード
│   ├── cashbook.py      #   出納帳モード
│   ├── journal.py       #   仕訳伝票モード
│   ├── csv_import.py    #   CSV明細取込
│   ├── ofx_import.py    #   OFX明細取込
│   ├── web_import.py    #   Web明細取込 (AI)
│   ├── ai_journal.py    #   AI証憑仕訳
│   ├── medical.py       #   医療費管理
│   ├── reports.py       #   レポート
│   ├── accounts.py      #   勘定科目管理 (JSON API)
│   ├── vouchers.py      #   証憑一覧 (電帳法検索要件対応)
│   ├── api.py           #   REST API (仕訳・AI証憑仕訳・証憑・Bearer認証)
│   ├── auditor.py       #   監査ダッシュボード・代理閲覧
│   ├── webauthn.py      #   Passkey WebAuthn API
│   ├── helpers.py       #   ビュー共通ヘルパー
│   └── settings.py      #   設定トップ・Passkey・外部AI・APIキー・月次確定・通知・監査アクセス管理
├── services/            # ビジネスロジック
│   ├── accounting.py    #   仕訳自動生成
│   ├── csv_import.py    #   CSVパース
│   ├── ofx_import.py    #   OFXパース
│   ├── ai_receipt.py    #   AI証憑解析・Web明細抽出 (OpenAI/Gemini/Claude/Ollama)
│   ├── audit.py         #   監査用アカウント (代理閲覧・権限・ロック・隠蔽)
│   ├── fiscal.py        #   月次確定・期間チェック・残高キャッシュ
│   ├── balance_cache.py #   確定済み残高キャッシュ管理
│   ├── seed.py          #   標準科目の初期データ
│   ├── tax.py           #   確定申告集計・月次比較・着地予測
│   ├── captcha.py       #   CAPTCHA 検証 (hCaptcha/reCAPTCHA/Turnstile/mCaptcha)
│   ├── notify.py        #   Webhook 通知 (Discord 等)
│   ├── voucher.py       #   ドラフト→証憑移行ヘルパー
│   ├── storage.py       #   ストレージ抽象レイヤー (local/S3)
│   ├── auto_import.py   #   自動取込オーケストレーター（内部利用）
│   └── sources/         #   外部ソースプロバイダー
│       └── webdav.py    #     WebDAV (Nextcloud 等)
├── forms/               # Flask-WTF フォーム
├── templates/           # Jinja2 テンプレート
└── static/              # CSS / JS
    ├── css/style.css     #   カスタムスタイル
    └── js/
        ├── app.js        #   PWA Service Worker 登録
        ├── drag_select.js #  ドラッグ選択 (バックトラック取消対応)
        ├── alpine-components.js # Alpine.js 共通コンポーネント
        └── webauthn.js   #   Passkey 認証
```

## テスト

```bash
# Docker 環境でテスト実行
docker exec -w /app server-web-1 python -m pytest tests/ -v

# GitHub Actions でも push/PR 時に自動実行されます
```

## ドキュメント

GitHub Pages でドキュメントサイトを公開しています: [https://nananek.github.io/iikanji-kakeibo/](https://nananek.github.io/iikanji-kakeibo/)

- プロダクト紹介
- REST API 仕様
- Python クライアントライブラリ

## ライセンス

本サーバー本体は [**Sustainable Use License v1.0**](./LICENSE) のもとで
配布されています (n8n プロジェクトと同等の条文を採用)。Copyright (c) 2026-
nananek。

### 運用形態別の取り扱い

- **自家用・内部利用** (個人または組織内、不特定多数の登録を受け付けない形態):
  Sustainable Use License の "internal business purposes" または
  "non-commercial or personal use" に該当し、自由に利用・改変・配布
  できます (再配布時はライセンス継承が必要)。新規登録は
  `REGISTRATION_ENABLED=false` で閉じることができます。
- **公開ホステッド提供** (不特定多数の登録を受け付ける形で SaaS として
  第三者に提供する場合): SUL の再配布条件は "free of charge for
  non-commercial purposes" に限定されています。非商用・無償での公開
  ホスティング (学術用途・コミュニティ運営等) は引き続き SUL の範囲内で
  可能ですが、**商用目的での提供** (有償・無償を問わず) は本ライセンス
  の範囲外です。商用 SaaS 提供については別途ライセンス取得が必要な
  ため、GitHub Issues 等でお問い合わせください。

### クライアントツール

`client-py` / `client-mcp` / `client-tui` の各クライアントは別リポジトリ
で **MIT License** で配布されています (本サーバー向け以外の用途にも自由に
利用可能)。

### 旧ライセンスについて

v3.x 系列までは「いいかんじ™ライセンス (IKL) v1.0」(MIT 互換) で配布されて
いました。v4.0 系列より上記の Sustainable Use License に移行しています。
旧版ソースコードは過去のタグからアクセス可能で、当時のライセンス条文が
適用されます。
