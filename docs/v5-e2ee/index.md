---
layout: default
title: v5.0 E2EE 設計書 (たたき台)
---

# v5.0 E2EE 設計書 — Epic [#84](https://github.com/nananek/iikanji-kakeibo/issues/84)

本書は v5.0 で「サーバが平文を一切持たない」E2EE 構成へ移行するための
**初版たたき台**。確定方針と未解決事項を区別して記述する。

---

## 確定事項 (2026-05-20 / 2026-05-22 追記)

| 項目 | 決定 |
|------|------|
| 暗号化範囲 | **全データ** (description / 摘要 / 金額 / 日付 / 科目コード / 証憑画像 / 設定 / API キー等すべて) |
| サーバの責務 | ストレージのみ。レポート集計・複式簿記検証はクライアント側 |
| 鍵管理の主軸 | **Passkey (WebAuthn PRF) 主 + パスフレーズフォールバック + リカバリシード (BIP-39 24 単語)** |
| 対応クライアント | Web (WebCrypto API) / `client-py` / `client-tui` |
| MCP サーバ | E2EE と本質的に両立不可 (LLM に平文を渡す) → **v5.0 で配布停止** |
| 自家ホスト LLM (`paid_llm` / `llama_cpp`) | E2EE と両立不可 → **v5.0 で廃止** |
| 移行戦略 | **一斉移行** (メンテナンスウィンドウ付。全ユーザーがパスフレーズ / Passkey 設定後に使える状態へ) |
| データ一括 zip ダウンロード | v5.0 で実装 (クライアントサイド復号 + バックグラウンド zip + メール通知) |
| **監査者連携モデル (2026-05-22)** | **(d) 非同期ワークフロー方式**。owner が auditor の公開鍵でスナップショットを暗号化して渡し、auditor は修正案を owner の公開鍵で暗号化して返す。MK は共有しない。同時編集は不可 (本来の税務顧問の在り方とも整合) |

---

## 段階的アプローチ

| Phase | 対象 | 目的 |
|-------|------|------|
| **E0** | 設計書 + プロトタイプ検証 | 暗号スキーム選定、WebCrypto / libsodium のベンチマーク、Passkey PRF 対応ブラウザ調査 |
| **E1** | 鍵管理基盤 | パスフレーズ → KDF → マスター鍵、Passkey PRF → マスター鍵、リカバリシード生成 |
| **E2** | API キー E2EE 化 | Fernet サーバ暗号化 → クライアント暗号化 (最小スコープで検証) |
| **E3** | 仕訳データ | JournalEntry / JournalEntryLine の暗号化 BLOB 化 |
| **E4** | 証憑画像 | クライアントで暗号化してから S3 / local にアップロード、サーバは暗号文 BLOB のみ保持 |
| **E5** | 監査連携 | 被監査者の鍵を監査者の公開鍵で鍵ラップして共有、ローテ可能化 |
| **E6** | クライアント全面対応 | Web / `client-py` / `client-tui` で全機能を E2EE 経由に |
| **E7** | 移行 + マイグレーション | 一斉移行のメンテナンスウィンドウ実行 |

---

## 1. 脅威モデル

### 守るもの

- 仕訳の摘要・金額・日付・科目コード等の **個人情報・経済情報**
- 証憑画像 (領収書・請求書・給与明細等の機密書類)
- API キー (外部 AI プロバイダの個人キー)

### 守らないもの (= サーバから見える)

- **ユーザーの登録メール (`users.email`)** — quota_warning / terms_update
  / account_deleted / contact_received / invitation 等の通知送信に
  必要。サーバが暗号化対象に含めるとメール送信機能が成立しない
  ため、本質的に平文保持。サインアップ時にユーザーが「メアドだけは
  運営者から見える」というリスクを受け入れる前提
- メタ情報: `created_at`, `last_login_at`, アクセスログ
- ユーザー ID (sequence)
- 暗号文のサイズ (~ 仕訳件数や仕訳の複雑さは漏れる)
- アクセスパターン (どの仕訳をいつ取得したか)

### メアドを完全に暗号化する場合の代替設計 (将来検討、Q13)

通知が必要な場合のオプション:
- (a) **HMAC-SHA256(email, server_secret)** (or Argon2id) を一意性キーに、
  平文はクライアント保持。通知は **クライアント側ポーリング + Web Push**
  で代替 (UX 劣化)。**生 `SHA-256(email)` は辞書攻撃で逆引き可能** な
  ためサーバ秘密キー付き HMAC か KDF (Argon2id) 必須
- (b) **二重メアド**: 「アカウント本人特定用 (暗号化)」+「通知用
  (平文、ユーザーが許可した別アドレス)」の二重持ち (UX 複雑)
- (c) **第三者通知サービス** (Pushover 等) と連携、サーバはメアドを
  持たず通知 token のみ保持 (外部依存)

v5.0 では (a)-(c) は採用せず、メアド平文保持で運用。

### メアド平文保持に伴うユーザー同意取得

サインアップ時 (`/register`) と既存ユーザーへの規約改訂時 (`/auth/accept-terms`)
で、メアドが平文でサーバ DB に保存されることを明示的に同意してもらう:

- **Privacy Policy への必須記載項目**:
  - 「登録メールアドレスは、通知メール送信のためサーバに平文で保管
    される (他の個人データは E2EE で暗号化されているが、メアドは例外)」
  - 「サーバ侵害時、メアドのみは漏洩リスクがある」
  - 「メアド漏洩リスクを許容できない場合は、サインアップしない
    or 退会して利用を終了する選択肢がある」
- **規約への必須記載項目**:
  - 「ユーザーは登録時にメアド平文保存に同意したものとみなす」
  - 「同意取消はアカウント削除 = 退会フローと等価」
- **同意 UI**:
  - `register.html` のチェックボックスに「メアドが通知用に平文保存され
    ることに同意する」を追加 (現状の利用規約同意とは別項目で明示)
  - 既存ユーザーは `CURRENT_TERMS_VERSION` 改訂で再同意フロー

### 想定脅威

1. **サーバ侵害**: DB 全件流出、ストレージ流出 → 暗号文のみで平文に戻せない
2. **サーバ内部犯**: 管理者がクエリで覗き見 → 平文を見られない
3. **MITM / プロキシ**: HTTPS 終端後のリバースプロキシで覗き見 → 暗号文のみ
4. **クライアント侵害**: ユーザー端末のマルウェア → **守れない** (前提から外す)
5. **パスフレーズ失念**: ユーザーの責任、リカバリシード未保管なら完全消失 (規約で明示)

---

## 2. 鍵管理アーキテクチャ

### 階層構造

```
[ユーザー認証要素]
├─ Passkey (主、WebAuthn PRF 拡張) ─→ derived_key_passkey ─┐
├─ パスフレーズ (フォールバック) ─→ Argon2id ─→ derived_key_pw ─┼─→ Master Key (32 bytes)
└─ リカバリシード (緊急用、BIP-39 24 単語) ─→ HKDF ─→ derived_key_recovery ─┘
                                                              │
                                                              ▼
                                                  暗号化対象データ (XChaCha20-Poly1305)
```

### マスター鍵 (Master Key, MK)

- **32 バイト** のランダムキー。初回設定時にクライアントで生成
- 認証要素 (Passkey / パスフレーズ / リカバリシード) **3 つすべてで MK をラップ**して保管
  - サーバには「暗号化された MK の Wrapped Copy」を 3 つ預ける
  - 認証要素のいずれか 1 つでアンラップして MK を復元できる
- **平文の MK はサーバに残らない**

### Wrapped Master Key の保管

```
wrapped_keys (擬似、詳細は §10.1):
| id | user_id | method        | webauthn_credential_id | wrap_iv | wrapped_master_key   | salt | kdf_params |
| 1  | 42      | passkey_prf   | 17 (FK PK)             | (12B)   | (ciphertext + tag)   | NULL | NULL       |
| 2  | 42      | passphrase    | NULL                   | (12B)   | (ciphertext + tag)   | (16B)| {m,t,p}    |
| 3  | 42      | recovery_seed | NULL                   | (12B)   | (ciphertext + tag)   | NULL | NULL       |
```

→ 実装着手レベルの詳細 (制約・partial UNIQUE INDEX・CASCADE) は **§10.1** 参照。

### 複数 Passkey のスケール

WebAuthn は複数 Passkey 登録をサポートしている (v4.x で既に対応)。
1 デバイス = 1 Passkey と仮定すると、Passkey を 3 台に登録した場合は
`wrapped_keys` に `(method='passkey_prf', webauthn_credential_id=17)`,
`(method='passkey_prf', webauthn_credential_id=23)`,
`(method='passkey_prf', webauthn_credential_id=42)` の 3 行が並ぶ
(`webauthn_credential_id` は `webauthn_credentials.id` PK の FK)。
追加挙動:
- 新規 Passkey 登録時: 既存セッションで MK を取得 → 新 Passkey の PRF
  で MK を再ラップ → 新行を INSERT
- Passkey 削除時: 該当 `wrapped_keys` 行も同時に削除 (ondelete=CASCADE)
- すべての Passkey 削除済 + パスフレーズなし + リカバリシードなし
  → アカウント実質ロック (退会フローへ誘導)

### Passkey PRF からの鍵派生

- WebAuthn 認証時に `extensions.prf.eval.first` を指定
  (WebAuthn PRF 拡張の `PRFValues.first` は `BufferSource` を期待する
  ため、実装時は `new TextEncoder().encode("iikanji-master-key-v1")`
  で UTF-8 バイト列に変換する)
- ブラウザ / 認証器が決定論的に 32 バイトの PRF 出力を返す
- これを HKDF-SHA256(input=PRF出力, salt=zero, info="iikanji-master-key-v1", L=32)
  で派生して `derived_key_passkey` とする
  - 確定方針: **HKDF を挟む** (info パラメータでドメイン分離。将来の鍵用途
    拡張 (例: AuditPackage 暗号化用の別 info) に備える)
- パスキー紛失時は **別の Passkey** か **パスフレーズ** か **リカバリシード** で MK を復元 → 新しい Passkey に再ラップ

### パスフレーズ

- **8 文字以上、推奨 16 文字以上** (BIP-39 シードなら強度十分)
- KDF: **Argon2id** (memory=64 MiB, iterations=3, parallelism=1) ※調整余地あり
- ソルトは `wrapped_keys.salt` (16 bytes random) で per-user

### リカバリシード

- BIP-39 形式の 24 単語フレーズ (256 bit エントロピー)
- 初回設定時に 1 回だけ画面表示 → ユーザーが紙にメモするまで dismiss 不可
- 再生成は MK アンラップ済 (= 既ログイン) 状態でのみ可能
- 紛失 = MK 復元不可能 (規約で警告)

---

## 3. 暗号スキーム

### 候補

| アルゴリズム | 提供 (Web) | 提供 (Python) | 認証付き | 備考 |
|------------|----------|-------------|---------|------|
| AES-256-GCM | WebCrypto 標準 | `cryptography` | ✅ | IV 96 bits、Nonce reuse 厳禁 |
| **XChaCha20-Poly1305** | libsodium.js (要追加) | `pynacl` | ✅ | Nonce 192 bits、Reuse 耐性高 |
| ChaCha20-Poly1305 | WebCrypto 非標準 | `cryptography` | ✅ | Nonce 96 bits |

### 推奨

**XChaCha20-Poly1305** (Nonce reuse 耐性が高く、暗号運用ミスに強い)。
ただし WebCrypto 標準ではないため libsodium.js (or `noble-ciphers`) を
バンドル必須。バンドルサイズ ~30-50 KB 増を許容する。

代替案: **AES-256-GCM**。WebCrypto 標準でバンドル不要。ただし Nonce
リユースに対する耐性は XChaCha20 の方が高い。Nonce はランダム生成 +
個別記録で運用すれば実害なし。

→ **暫定: AES-256-GCM** を選択 (WebCrypto 標準でクライアント実装が楽)。
プロトタイプで XChaCha20 と比較してから最終決定。

### Nonce 衝突確率の注釈 (AES-256-GCM 採用時)

AES-256-GCM の Nonce は 96-bit。ランダム生成すると Birthday Paradox
により以下の確率で衝突が発生する (`P ≈ 1 - exp(-n² / 2^97)`):

| n (暗号化回数) | 衝突確率 |
|--------------|---------|
| 2^32 (約 43 億) | ~10^-10 (無視できる) |
| 2^44 (約 17.6 兆) | ~0.2% |
| 2^46 | ~3% |
| **2^48 (約 281 兆)** | **~39%** |

NIST SP 800-38D はランダム IV の invocation 上限として **2^32** を
推奨している。家計簿用途では、1 ユーザーの仕訳行 (JournalEntryLine)
を全部数えても 2^32 件未満が現実的上限。E2 / E3 で大量データの
ベンチマークを取った上で、**1 MK 当たり 2^32 件超は MK ローテーション**
を推奨するガイドラインを設ける。

### Argon2id の WASM バンドル

Argon2id は WebCrypto に標準実装がない。ブラウザ側でパスフレーズから
鍵派生するには `argon2-browser` (WASM ビルド、~30 KB gzipped) を別途
バンドルする必要がある。libsodium.js を使う場合は同梱の Argon2id
実装を使える (XChaCha20 採用時に同時解決)。

---

## 4. データモデル変更

### 暗号化対象テーブル

```
journal_entries:    date / description / source / batch_id → 全部 ciphertext blob 化
journal_entry_lines: account_code / debit_amount / credit_amount / description → 同上
medical_expenses:   patient_name / hospital_name / amount_paid 等 → 同上
vouchers:           image_key (path) は平文、画像本体は s3 / local 上で暗号化済
voucher_audit_logs: detail フィールドは暗号化、action は平文 (フィルタ用途)
ai_drafts:          全フィールド暗号化
user_ai_configs:    api_key_encrypted は **クライアント側で暗号化** (Fernet 廃止)
```

### 暗号化されない情報 (= サーバから見える)

- `users.id`, `users.created_at`
- **`users.email`** — 通知送信のため平文保持必須 (§1「守らないもの」
  参照、Q13 で代替設計を検討中)
- 各テーブルの `id` (連番)
- 各テーブルの `created_at`, `updated_at` (タイムスタンプ)
- `user_id` 外部キー (テナント分離のため必須)
- `file_hash` (SHA-256、内容を直接漏らさない一意性キー)

### Webhook 通知 (notify.py) と E2EE の非両立

現行 `notify.py` / `WebhookConfig` は仕訳登録時に摘要・金額を
Discord 等に **平文で送信** している。E3 で仕訳が暗号化されると
サーバには平文がないため、以下のいずれかを E3 で確定する:

- (a) **Webhook 自体を v5.0 で廃止** (シンプル、機能後退)
- (b) **暗号文のままメタ情報のみ通知** (日時 / 何らかの URL リンク。
  内容はクライアントで取得・復号して確認)
- (c) **サーバ側での一時復号を許可** (E2EE の保証が部分的に弱まる、
  非推奨)

暫定: (a) で計画 (b) も併用しうる。

### 検索可能性

- **日付・金額での絞り込み検索は不可** (全暗号化のため)
- クライアント側で全件取得して JS / Python で絞り込み
- 大量データのユーザーは UI 体感が遅くなる → ページネーション + 直近 N 件先読み戦略
- レポート (P/L, B/S) はクライアントで全件取得 → 集計

---

## 5. クライアント実装方針

### Web (WebCrypto API + Alpine.js / Vue)

- 暗号化処理は **専用 Web Worker** に閉じ込める (`/static/js/crypto-worker.js`)
- マスター鍵は Worker クロージャ内のみに保持し、`window.*` には**絶対に露出しない**
  (`window.IIKANJI_MK` 等のグローバル変数禁止 — XSS が 1 件でも決まれば全データ漏洩のため)
- メインスレッドは `postMessage` で暗号化/復号の **結果だけ** を受け取る
- Worker は鍵を保持しつつメッセージごとに encrypt/decrypt のみ応答するシンプルな実装
- Service Worker キャッシュ・IndexedDB には **暗号文のみ** 保存可 (オフライン対応)
- リロード時は Passkey / パスフレーズで再認証して Worker を再構築

### client-py / client-tui

- `cryptography` ライブラリ (PyCA cryptography or pynacl)
- 鍵は `~/.cache/iikanji/master_key` に **OS のセキュアストレージ** (keyring) で保管
- パスフレーズ入力はインタラクティブ (`getpass.getpass()`)
- Passkey PRF は CLI で扱えないため、パスフレーズフォールバック必須

### MCP サーバ (`client-mcp`)

- **v5.0 で配布停止**
- 理由: LLM (Claude Desktop 等) に平文を渡すため、E2EE の前提が崩れる
- README に廃止予定を明記、v4.x は引き続き利用可

---

## 6. 移行戦略 (一斉移行)

### スケジュール

1. **v5.0-beta リリース 1 ヶ月前**: 既存ユーザー全員にメール通知
   - 「v5.0 で E2EE 化、Passkey + パスフレーズ + リカバリシードの設定が必要」
   - 「メンテナンスウィンドウ X 月 X 日 hh:mm-hh:mm」
   - 「未設定で放置するとアカウントが利用できなくなる」
2. **メンテナンスウィンドウ**:
   - 全テーブルを **平文 → 暗号化済 BLOB** に移行するマイグレーション実行
   - ユーザーごとに一時的に **サーバ生成のマスター鍵** で暗号化 (= サーバが鍵を一時保持)
   - 移行完了後、ユーザーが初回ログインで Passkey / パスフレーズ設定 → サーバ生成鍵で復号 → 自分の鍵で再暗号化 → サーバから一時鍵を破棄
3. **猶予期間 (例: 30 日)**: 鍵設定未完了ユーザーはログイン時にダイアログ強制
4. **猶予期間後**: 鍵設定未完了ユーザーは「鍵設定完了するか退会するか」を選択

### 移行スクリプトの設計

- 各テーブルに `is_encrypted` カラム追加 (移行進捗フラグ)
- マイグレーションは段階的: 1000 ユーザーずつバッチ処理
- 失敗時のロールバックは「サーバ生成鍵で復号 → 平文に戻す」(逆操作可能なので)

### サーバ生成鍵の扱い

**最大の妥協点**。一斉移行のためサーバが一時的にマスター鍵を保持する
期間が発生する。鍵設定完了後にサーバから破棄するが、その間にサーバ侵害
があれば過去データは漏れる。
代替案: 「クライアント側でユーザーが自分の鍵を設定するまでサービス
停止」も検討。UX 重視で前者採用。

---

## 7. 監査者連携

> **実装状況 (2026-06)**: モデル (d) 非同期ワークフロー方式が E5 ([#112](https://github.com/nananek/iikanji-kakeibo/issues/112)) で実装完了。旧リアルタイム代理閲覧 (`acting_as_user_id` / モデル a-c) は撤去済み。利用者向けの移行手順は **[監査連携の移行ガイド](audit-migration.html)** を参照。

監査者連携には大きく 2 つのモデルがある:

- **鍵共有モデル (a/b/c)**: owner の MK を auditor に渡す。auditor がログインして
  リアルタイム閲覧。現行 v4.x と同じ UX
- **ワークフローモデル (d)**: owner が「税務科目限定スナップショット」を auditor
  の公開鍵で暗号化して送る。auditor は自分の鍵だけで作業し、修正案を owner の
  公開鍵で暗号化して返す。MK は共有しない

### モデル (d): 非同期ワークフロー方式 (推奨案)

**核となる考え方**: 鍵を共有せず、データを公開鍵暗号で一方向に橋渡しする。
税理士業務は実務的にも「期末に帳簿を渡して見てもらう」非同期パターンが普通で、
リアルタイム共有は実は珍しい。

#### 前提

- 全ユーザー (owner / auditor 問わず) が X25519 公開鍵 / 秘密鍵ペアを生成
  (公開鍵は `users.public_key` に平文保管、秘密鍵は MK で AES-GCM 暗号化して
  `users.encrypted_private_key` + `users.private_key_iv` に保管)。生成タイミング
  は MK 初回設定時、既存 MK ユーザーは MK 解錠時に lazy backfill (E5 PR-A 実装)
  - 注: 当初設計では秘密鍵を `wrapped_keys` に置く案だったが、`wrapped_keys` は
    認証要素で MK 本体をラップするテーブル (CHECK/UNIQUE/credential 依存) で
    スキーマが合わないため、`public_key` と並ぶ `users` 列に置く方式に変更した
    (他の E2EE レコードと同じ「MK で AES-GCM 暗号化」パターンに一致)
  - `/api/v1/keypair` は**常にログイン本人の self-service**。監査代理閲覧
    (acting-as) 中は 403 で遮断する。これがないと Lv3 監査者が代理閲覧中に
    オーナーの `public_key` を自分の鍵にすり替えられ、後続 PR で生成する
    監査パッケージを盗み見できる (鍵注入攻撃) ため (E5 PR-A 実装)
- AuditGrant は「監査依頼」のトリガーであり、鍵共有を意味しない

#### フロー (Lv2 監査の例)

```
[owner クライアント]
1. 月次確定済の仕訳を MK でローカル復号
2. 税務科目に該当する仕訳のみフィルタ (owner クライアントで強制)
3. auditor の公開鍵 (X25519) でハイブリッド暗号化
   - ephemeral key + AES-256-GCM
4. AuditPackage としてサーバにアップロード (snapshot_hash も同時保存)

[auditor クライアント]
5. 「監査依頼」一覧で AuditPackage を取得
6. 自分の秘密鍵で復号 → 修正案を作成 (auditor の MK で自分のスクラッチに保存)
7. 修正案を owner の公開鍵で暗号化 → AuditResponse としてサーバに送信
   (「修正案」 or 「差戻し」の 2 種)

[owner クライアント]
8. AuditResponse を取得 → 自分の秘密鍵で復号
9. 修正案を確認 → 反映 (= 自分の MK で再経理して帳簿に保存)、または差戻しを
   受けて手作業 (= 何もしない)
```

#### Lv1/Lv2/Lv3 への対応

- **Lv1 (集計のみ)**: owner がレポート結果スナップショット (P/L / B/S /
  月次比較) だけを auditor の公開鍵で暗号化して送る。仕訳は渡らない
- **Lv2 (税務科目限定)**: owner が税務科目フィルタを適用した仕訳セットだけを
  auditor の公開鍵で暗号化して送る。**owner クライアント側で強制** するので
  E2EE と矛盾しない (owner の意思によるフィルタ)
- **Lv3 (本人代理に近い)**: 全仕訳スナップショット送信 + 修正案戻し。
  原理的に同じフローで実現可能。MK は共有しない

#### 利点

- E2EE が技術的に純粋: owner / auditor それぞれが自分の MK しか持たない
- Lv2 (科目限定) の保証が **owner 側のエクスポート時に強制可能** —
  サーバや auditor クライアントの善意に頼らない
- 「監査者がデータを見られる範囲」が常に owner の意思に従う
- 同時編集問題なし (シーケンシャルワークフロー)
- 監査の前にスナップショット hash を取れば「いつ何を渡したか」の証跡が残る
  (改ざん検出 + 電帳法的監査証跡)
- 鍵ローテーション不要: 権限取消は単に「次の AuditPackage を作らない」だけ

#### 欠点

- **リアルタイム閲覧不可** (税理士が「ちょっと見せて」というオンデマンド閲覧
  ができない)
- v4.x の Lv2 UX (auditor がログインして閲覧する形式) とは大きく異なる
- 修正案のマージは owner の手動操作 (auditor が直接書き込めない)
- 修正案の差分表示・コンフリクト解決の UI が必要 (差分 UI を新設)

#### 未確定の設計課題 (E3 / E5 で確定)

PR #120 review で指摘された 4 つの設計ギャップ。E3 (データモデル) 着手前に
方針を決める必要がある。

##### (i) 公開鍵の真正性検証 (MITM 耐性)

`users.public_key` はサーバが平文管理する。サーバが侵害された場合、auditor
の公開鍵をすり替えてスナップショットを傍受される可能性がある。完全な E2EE
を謳うには鍵検証フローが必要。候補:

- **TOFU (Trust On First Use)**: 初回の AuditGrant 確立時に公開鍵を
  owner クライアントが pinning。以降サーバが返す公開鍵が変わったら警告
- **帯域外 fingerprint 確認**: 公開鍵の指紋を別チャネル (対面 / 電話 / 紙)
  で確認させる UI を提供
- **Web of Trust 風の署名連鎖**: 過剰、本プロジェクトでは不採用

→ **暫定: TOFU + fingerprint 表示** を E5 で実装。owner 設定画面で auditor
の公開鍵 fingerprint を確認・承認するフローを設ける。

##### (ii) 送付済み AuditPackage のフォワードセクレシー

auditor の秘密鍵が将来漏洩した場合、サーバに残存する過去の AuditPackage が
すべて復号される。候補:

- **保持期間 TTL**: AuditPackage / AuditResponse に明示的な expiry を設け、
  期限切れでサーバから自動削除 (例: 監査完了から 90 日)
- **Ephemeral ECDH によるセッション鍵 (完全前方秘匿性)**: パッケージごとに
  ephemeral X25519 鍵ペアを生成し、ECDH 後にセッション鍵を導出。送付完了で
  ephemeral 秘密鍵を破棄。auditor の長期鍵を使わない設計
- **両立**: TTL + ephemeral ECDH の併用が望ましい

→ **暫定: ephemeral ECDH (per-package) + TTL 90 日** を E3 のデータモデルに
組み込む。HPKE (RFC 9180) を流用すると実装が楽。

##### (iii) AuditPackage / AuditResponse のサーバ側ストレージ

- **保持期間**: TTL 90 日想定 (上記 (ii) と整合)
- **アクセス制御**: owner / auditor のみが GET 可能、他ユーザーは 404
- **サーバ上での追加暗号化**: ハイブリッド暗号文なのでサーバ側追加暗号化は
  不要。ただし `image_key` 相当の path (もしあれば) は平文
- **データモデル**: `audit_packages` / `audit_responses` テーブルを新設。
  E3 設計時に確定

##### (iv) マルチラウンドレビューの扱い

owner が修正反映後に再スナップショットを送るケースを想定。プロトコルレベル
の番号付け:

- `audit_packages.round_id` (連番、AuditGrant 内で一意)
- AuditResponse は対応する round_id を参照
- auditor は最新 round のみ作業可能 (古い round は read-only)
- コンフリクト検出: owner が新 round を送る前に未処理の AuditResponse が
  あれば警告

### 候補比較

| 案 | 鍵共有 | 技術的強制 | UX 変更 | 実装複雑度 |
|---|-------|----------|--------|----------|
| (a) Lv2 廃止 → Lv1/Lv3 のみ | Lv3 で必要 | Lv1 は OK | 小 (Lv2 廃止のみ) | 小 |
| (b) Lv2 を契約上の取り決めに降格 | Lv2/Lv3 で必要 | なし (善意に依存) | なし | 小 |
| (c) MK 分離 + 科目別暗号化レイヤ | 部分共有 | 強制可能 | 小 | **大** |
| **(d) ワークフロー方式 (推奨)** | **共有しない** | **owner 側で強制** | **大 (非同期化)** | 中 |

**(d) 推奨理由**:

1. E2EE と監査制度の論理的矛盾を、UX 設計の変更で解消できる
2. そもそも **同時編集は本来の税務・会計顧問のあり方ではない**。税理士業務は
   「帳簿を作成・確定 → 顧問が確認・修正案を提示 → 本人が反映」という
   シーケンシャルなプロセスであり、リアルタイム共有は実務的にも不要
3. (a/b/c) はいずれも「鍵共有」を前提とした実装上の妥協であり、E2EE の
   純粋性を犠牲にする。(d) はこの妥協を不要にする

### 鍵共有モデル (a/b/c) の詳細 — (d) を採用しない場合の参考

#### (a/b/c) 共通: 鍵ラップ方式

- 監査者は登録時に自分の **公開鍵 / 秘密鍵** ペアを生成 (X25519 推奨)
- 被監査者 (owner) が監査者 (auditor) に AuditGrant を発行するとき:
  1. owner が自分のマスター鍵を auditor の公開鍵で暗号化 (=「鍵ラップ」)
  2. ラップされた鍵を `audit_grant_keys` テーブルに保存
  3. auditor がログイン時に自分の秘密鍵でアンラップ → owner の MK を取得 →
     owner のデータを復号

#### 権限取り消し (a/b/c 採用時)

- `AuditGrant.revoked_at` をセットしても、auditor は過去に取得した平文を
  持っている可能性
- 取り消し時は owner が **マスター鍵をローテーション** (= 全データを新しい鍵で
  再暗号化) する必要
- 再暗号化は重いバッチ処理 → 取り消し時の UX 警告

### 決定タイミング

E1 終了前 (E3 設計の前提条件)。E3 (仕訳暗号化) のデータモデルが採用案に依存
するため、未確定だと E3 設計が固まらない。

### 詳細は別途設計

監査者 UX は E5 フェーズで詳細検討 (差分表示・コンフリクト解決 UI 含む)。

---

## 8. リカバリ

### BIP-39 24 単語

- エントロピー 256 bit
- 単語リスト英語版 (日本語版もあり、要検討)
- 初回設定時に 1 回だけ画面表示 + チェックボックス「紙にメモした」必須
- 印刷用 PDF テンプレも提供

### リカバリ手順

1. ログイン画面で「リカバリシードでログイン」リンク
2. 24 単語入力 + 新しいパスフレーズ設定
3. シードから derived_key_recovery 派生 → wrapped_keys から MK 復元 → 新パスフレーズで再ラップ → 古いシードを無効化 (recovery seed は 1 回限り使用、新シードを生成)

### 紛失時

- リカバリシード + Passkey + パスフレーズの **すべて** を失った場合、データ復元不可能
- 規約で「データ復元不可」「運営者でも復元できない」を明示
- 退会フローを案内 (アカウントを削除して再登録)

---

## 9. 未解決の質問

| # | 項目 | 検討状況 |
|---|------|----------|
| Q1 | 暗号スキームは AES-256-GCM か XChaCha20-Poly1305 か | E0 プロトタイプで決定 |
| Q2 | Passkey PRF 非対応ブラウザ (Safari の古いバージョン等) でパスフレーズ強制になる UX | 開発中に試験 |
| Q3 | レポート計算をクライアント側で行う際の大量データのパフォーマンス | E3 で実測 |
| Q4 | Lv1/Lv2 監査者の暗号設計 | **(d) ワークフロー方式採用が暫定確定 (2026-05-22)** — 鍵共有せず公開鍵暗号でスナップショットを橋渡し。詳細 UX は E5 で詰める |
| Q5 | バックアップ・整合性監査の運用変更 (平文を見られないので運用者は監視できない) | E6 で運用ドキュメント更新 |
| Q6 | エクスポート機能 (zip ダウンロード) のバックグラウンドジョブ実装 | E6 で実装 |
| Q7 | クライアント間 (Web / Python / TUI) の暗号スキーム互換性テスト | E6 で結合テスト |
| Q8 | 移行時のサーバ生成鍵の保持期間と運用 | E7 で精緻化 |
| Q9 | REST API Bearer トークン (OAuthToken) と鍵階層の整合 — クライアントがどう MK を取得するか | E1 / E2 で確定 |
| Q10 | ページリロード時の再認証 UX (毎回 Passkey タップ強制になるか、Session-local Worker 維持等) | E0 / E1 プロトタイプで検証 |
| Q11 | E4 後の `file_hash` ハッシュ検証は誰がやるか (現行はサーバが SHA-256 計算 + VoucherAuditLog 書込)。クライアント復号後に検証してサーバへログ送る形に変えるか、暗号文の SHA-256 を別途記録するか | E4 で確定、電帳法改ざん防止証跡との整合性確認 |
| Q12 | Web Worker の postMessage 型検証 — 不正メッセージで Worker クラッシュ / 鍵漏洩しない設計 | E0 プロトタイプで検証 (Zod 等のスキーマ検証検討) |
| Q13 | メアド平文保持の代替設計 (ハッシュ化 + Web Push、二重メアド、Pushover 等) — v5.0 は平文保持で運用、v5.x 以降で再評価 | v5.x で再評価 (現状は §1「守らないもの」として明示) |

---

## 10. E1 設計スケッチ (鍵管理基盤の詳細)

E0 完了 (#107) を経て E1 (#108) 着手前の設計スケッチ。§2 が概念レベルなのに
対し、本節は実装着手レベルの詳細を扱う。

### 10.1 `wrapped_keys` テーブル

```
wrapped_keys
| カラム                    | 型           | 制約                                       | 用途 |
|------------------------- |-------------|------------------------------------------|------|
| id                       | bigserial   | PK                                       | |
| user_id                  | bigint      | FK users.id ON DELETE CASCADE            | |
| method                   | text        | NOT NULL, CHECK (method IN ('passkey_prf','passphrase','recovery_seed')) | enum 相当 |
| webauthn_credential_id   | bigint      | NULL, FK webauthn_credentials.id ON DELETE CASCADE | method=passkey_prf 時のみ。`webauthn_credentials.id` (PK) を参照 |
| wrapped_master_key       | bytea       | NOT NULL                                 | AES-256-GCM ciphertext (タグ含む。**IV は別カラム**) |
| wrap_iv                  | bytea       | NOT NULL                                 | wrap 時の IV (12B) |
| salt                     | bytea       | NULL                                     | method=passphrase 時の per-user salt (16B、Argon2id 用)。method=recovery_seed では NULL (HKDF の入力は mnemonic UTF-8 バイト列, salt=zero) |
| kdf_params               | jsonb       | NULL                                     | method=passphrase 時のみ。`{memory, iterations, parallelism}` (Argon2id) |
| created_at               | timestamptz | NOT NULL                                 | |
| last_used_at             | timestamptz | NULL                                     | アンラップ成功時に更新 |
| label                    | text        | NULL                                     | UI 表示用 (例: "iPhone 14 Pro Passkey") |

-- UNIQUE 制約 (PostgreSQL の NULL 仕様に対応した partial index で実装)
CREATE UNIQUE INDEX uq_wrapped_keys_passkey
  ON wrapped_keys (user_id, method, webauthn_credential_id)
  WHERE webauthn_credential_id IS NOT NULL;
CREATE UNIQUE INDEX uq_wrapped_keys_password_recovery
  ON wrapped_keys (user_id, method)
  WHERE webauthn_credential_id IS NULL;
CREATE INDEX ix_wrapped_keys_user_id ON wrapped_keys (user_id);
```

- `wrapped_master_key` は **タグ込みの ciphertext** のみを格納し、**IV は `wrap_iv` カラムに分離**。`concat(iv || ciphertext)` の連結保管はしない (実装者の混乱防止)
- `webauthn_credentials` PK (`id`) を FK 参照。`webauthn_credentials.credential_id` (bytea, UNIQUE) ではなく PK 参照にすることで `ON DELETE CASCADE` が DB レベルで効く
- `last_used_at` のセマンティクスは `webauthn_credentials.last_used_at` (WebAuthn 認証成功時に更新) と区別される: **`wrapped_keys.last_used_at` は MK アンラップ成功時のみ更新**。同じ Passkey でも WebAuthn 認証は成功したが PRF で MK アンラップに失敗するケース (例: PRF 非対応端末) を分けて記録できる
- パスフレーズ / リカバリシードは 1 ユーザー 1 行のみ (上記の **partial UNIQUE INDEX** で強制)。
  PostgreSQL は `NULL ≠ NULL` のため通常の `UNIQUE (a, b, c)` では NULL を含む
  カラムで複数行が共存できてしまうので、`WHERE` 句で 2 つに分けた partial
  index にする必要がある
- **マイグレーション番号は仮**。現行 develop の最新 (現時点で `045_voucher_active_partial_index`) の次番号に合わせて確定する

### 10.2 MK 生成シーケンス (新規ユーザー / 移行ユーザー)

```
client                              server
  |                                  |
  | 1. crypto.getRandomValues(32B)   |
  |    → MK (Worker クロージャに保持) |
  |                                  |
  | 2. (Passkey 登録) 認証ダイアログ |
  |    PRF eval → derived_key_passkey|
  |                                  |
  | 3. AES-GCM(derived_key, MK)      |
  |    → wrapped + iv                |
  |                                  |
  | 4. POST /api/v1/wrapped-keys     |
  |--------------------------------->|
  |                                  | INSERT wrapped_keys
  |<---------------------------------|
  |                                  |
  | 5. 同様に passphrase / recovery_seed もラップして保存 |
```

⚠️ **移行フェーズ限定の例外**: 設計書 §6 (移行戦略) のサーバ生成鍵フェーズでは、
サーバが一時的に MK を保持してデータを暗号化する。これは E2EE のゼロ知識
モデルの**一時的な例外**であり、その期間はサーバ侵害で過去データが漏れる
リスクがある。手順:

1. メンテナンスウィンドウ中、サーバが一時 MK を生成してデータを暗号化
2. ユーザーが鍵設定完了 (本フローで自分の wrapped_keys を登録)
3. クライアントが一時 MK で復号 → 自分の MK で再暗号化
4. **完了後、サーバから一時 MK を即時削除** (`mk_rotation_state` で進捗管理、
   全データの再暗号化完了をサーバが確認した上で削除)
5. 移行 SLA: 鍵設定完了から N 日以内に再暗号化完了 (§6 で精緻化)

### 10.3 MK アンラップシーケンス (ログイン後)

```
client                              server
  |                                  |
  | 1. ログイン認証 (Bearer or session)|
  |                                  |
  | 2. GET /api/v1/wrapped-keys      |
  |--------------------------------->|
  |<-- [{id, method, webauthn_credential_id, wrapped_master_key, wrap_iv, salt, kdf_params, label}, ...] |
  |   (API レスポンスのフィールド名は §10.1 の DB カラム名と一致させる) |
  |                                  |
  | 3. クライアントが利用可能な要素を選択 |
  |    a. Passkey → PRF eval (32B) → HKDF-SHA256(salt=zero, info="iikanji-master-key-v1") → derived_key |
  |    b. パスフレーズ → Argon2id (kdf_params + salt 使用) → derived_key |
  |    c. リカバリシード (BIP-39 24 単語) → 下記 A案 で derived_key 派生 |
  |    ※ 各要素の KDF は 10.1 表に従う。§2「Passkey PRF からの鍵派生」と整合 |

リカバリシード KDF: **A案 (ニーモニック文字列直接 HKDF)** を採用。

- ニーモニックを UTF-8 バイト列に変換 (例: "abandon abandon ... art" → bytes)
- HKDF-SHA256(input=bytes, salt=zero, info="iikanji-recovery-key-v1", L=32)
- 派生した 32B を derived_key として使用

理由: BIP-39 標準フロー (B案: ニーモニック → PBKDF2-HMAC-SHA512 → 512bit シード
→ HKDF) はハードウェアウォレットとの相互運用のためのもの。本サービスは独立した
リカバリ用途であり相互運用不要なので、A案でシンプルに統一する。Argon2id を
挟まないのは BIP-39 24 単語のエントロピー (256 bit) が十分高く、辞書攻撃に
耐性があるため。

**入力時の BIP-39 チェックサム検証は必須**。24 単語 = 256 bit エントロピー +
8 bit checksum (`SHA-256(entropy)` の **先頭 8 bit = 先頭 1 バイト**、
[BIP-0039](https://github.com/bitcoin/bips/blob/master/bip-0039.mediawiki))。
クライアント側で入力時にチェックサム検証を行い、不一致なら「入力ミスです」と
即時エラー表示 (HKDF を回して「復号失敗」と出るより親切)。これにより
「シードが違う」か「入力ミス」かをユーザーに伝えられる。
  |                                  |
  | 4. derived_key で wrapped を unwrap |
  |    → MK を Worker クロージャに    |
  |                                  |
  | 5. PUT /api/v1/wrapped-keys/<id>/touch (last_used_at 更新) |
  |--------------------------------->|
```

- アンラップ失敗 (タグ検証 NG) は「鍵が間違っている」を示す。攻撃検知では
  ないので一般エラーで返す
- リロード時の MK 揮発対策は SharedWorker による永続化で解消 (§10.7
  Q10 整理参照)。タブが 1 つでも生きていれば再アンラップ不要

#### PRF 非対応環境のフォールバック (Q2)

Passkey PRF (`extensions.prf`) は 2026 年時点で Chrome / Edge / Firefox 系は
対応、iOS Safari は OS バージョン依存。クライアントは PRF 利用可否を起動時に
検出し、非対応なら以下のフォールバック:

```
1. WebAuthn 認証成功時に extensions.prf.results が undefined → PRF 非対応と判定
2. 「この端末では Passkey 単独で MK を復元できません」と UI 通知
3. ユーザーにパスフレーズ入力を要求 → Argon2id で derived_key 派生 → MK アンラップ
4. 同セッション中の以後の操作は MK 保持済 (= UX は実質変わらない)
5. wrapped_keys は変更しない (Passkey PRF 用の行を残しておくと PRF 対応端末で
   別途使えるため)
```

非対応端末では「Passkey ボタン押下 → 失敗 → パスフレーズ強制」のひと手間
が発生するが、移行は強制しない (PRF 対応端末からの利便性を確保)。

### 10.4 認証要素の追加・削除

#### 追加 (例: Passkey 追加登録)

前提: 既存セッションで MK を保持済み。

```
1. WebAuthn navigator.credentials.create() で新 Passkey 登録
2. 直後に navigator.credentials.get() で PRF eval を取得
3. AES-GCM(derived_key_new, MK) → wrapped_new
4. POST /api/v1/wrapped-keys (新行 INSERT)
5. webauthn_credentials にも新 credential を登録 (現行 v4.x のフロー)
```

#### 削除 (例: Passkey 削除)

```
1. webauthn_credentials の該当行を削除
2. CASCADE で wrapped_keys の対応行も削除
3. 残存 wrapped_keys の件数チェック
   - 0 件になる場合は「最後の認証要素を削除すると復号不能になる」を警告し中止
   - 1 件以上残るなら削除確定
```

UI 制約 + **サーバ側強制**: 「Passkey 全削除 + パスフレーズなし + リカバリシード
なし」状態への遷移は禁止 (= 復号不能状態への自分での誘導を防ぐ)。クライアント
側の UI チェックは bypass 可能なため、`DELETE /api/v1/wrapped-keys/<id>` の
サーバ実装でも「削除後に wrapped_keys 件数が 0 になる場合は 409 Conflict
を返す」ガードを必須化する。

#### リカバリシード使用後の再生成 (§8 連動)

§8 の「recovery seed は 1 回限り使用、新シードを生成」を実装フローに落とす:

```
1. リカバリシード入力で MK アンラップ成功
2. 即時に旧 wrapped_keys (method='recovery_seed') 行を DELETE
3. 新しい BIP-39 24 単語を生成 → HKDF で derived_key_new 派生
4. derived_key_new で MK を wrap → 新 wrapped_keys (method='recovery_seed') 行を INSERT
5. ユーザーに新シードを 1 回だけ表示 (ユーザーが紙にメモするまで dismiss 不可)
```

これにより、同じシードが流出した場合の二次被害を防ぐ (パスフレーズ漏洩時の
パスフレーズ変更フローと同等の扱い)。

### 10.5 MK ローテーション

#### 起動条件

- 認証要素のいずれかが侵害された (パスフレーズ漏洩 / Passkey 紛失)
- ユーザーが任意で実行

#### 原子性の保証

バッチ再暗号化は全件を 1 トランザクションで完了できない (データ量大)。
途中失敗から復旧可能にするため、`users.mk_rotation_state` (jsonb) を追加し、
**MK_old と MK_new の wrap を両方持つ "オーバーラップ期間"** を設ける:

```
users.mk_rotation_state (jsonb, nullable):
{
  "status": "rotating",       // null=非ローテ中 / rotating / verifying / committed
  "started_at": "2026-05-22T08:00:00Z",
  "progress": {"total": 100000, "done": 23456, "unit": "journal_entry_lines"},
  "new_wrapped_keys_id_set": [101, 102, 103],
  "rotation_token_hash": "<SHA-256 of token, hex>",  // X-Rotation-Id ヘッダの SHA-256 と照合 (Bearer と同じパターン、DB 漏洩時の乗っ取り耐性)
  "auto_abort_at": "2026-05-29T08:00:00Z"  // 7 日後に自動 abort (運用安全策)
}

// rotation_token のライフサイクル:
//  - ローテ開始 (POST /api/v1/wrapped-keys/rotate/begin) 時にサーバが生成、
//    raw token はレスポンスで 1 回だけクライアントへ返却。サーバには
//    SHA-256 ハッシュのみ rotation_token_hash として保存 (oauth_tokens.token_hash
//    と同じパターン)
//  - 以後の PUT (再暗号化) では X-Rotation-Id ヘッダ必須、サーバはヘッダ値の
//    SHA-256 と rotation_token_hash を比較。一致しない PUT は 423 Locked
//  - auto_abort_at まで有効。commit / abort で破棄
```

#### フロー (再開可能設計)

```
client                              server
  |                                  |
  | 1. MK_new = crypto.getRandomValues(32B)|
  |                                  |
  | 2. 全認証要素で MK_new をラップ      |
  |    POST /api/v1/wrapped-keys (新行を追加 INSERT、旧行は残す)|
  |--------------------------------->|
  |                                  | INSERT + UPDATE users.mk_rotation_state = {status:"rotating", ...}
  |                                  |
  | 3. データ全件を順次 GET → 復号 → 再暗号化 → PUT |
  |    PUT 時に rotation_id をヘッダ付与 (冪等性確保)|
  |--------------------------------->|
  |                                  | progress 更新
  |    ※ 途中クラッシュ → 再開時は mk_rotation_state.progress から再開 |
  |    ※ 各レコードは MK_old で復号 → タグ検証成功なら MK_new で再書き込み |
  |    ※ 既に MK_new 済のレコードは MK_old で復号失敗 → スキップ (冪等) |
  |                                  |
  | 4. 完了 → POST /api/v1/wrapped-keys/rotate/commit |
  |--------------------------------->|
  |                                  | トランザクション (user_id フィルタを厳格適用):
  |                                  |   DELETE wrapped_keys WHERE user_id=current_user.id AND id NOT IN new_set
  |                                  |   UPDATE users.mk_rotation_state = NULL
  |                                  |   ※ サーバは new_set の id も user_id=current_user.id を |
  |                                  |     満たすかをトランザクション内で再検証 (IDOR 防止)        |
  |                                  |
  | 5. 失敗時 (commit 前) /api/v1/wrapped-keys/rotate/abort |
  |--------------------------------->|
  |                                  | ロールバック: 新 wrapped_keys 行 (MK_new wrap) を |
  |                                  | DELETE。データ本体は MK_old で復号可能なまま (再暗号化が |
  |                                  | 未完了なので "復元" 操作不要)。users.mk_rotation_state=NULL |
```

#### 失敗パターン

- **クライアントクラッシュ中**: 次回ログイン時に `mk_rotation_state.status=rotating`
  を検知 → 「ローテーション再開しますか / 中止しますか」UI 表示
- **commit 直前で失敗**: 旧 wrapped_keys が残るので旧 MK でアンラップ可能
- **commit 中で失敗**: トランザクションロールバックで旧状態維持
- **ローテーション中の他クライアント書き込み**: `users.mk_rotation_state.status=rotating` 中は書き込み API を 423 Locked で拒否。ただし **ローテーション自身の PUT** は `X-Rotation-Id: <token>` ヘッダで識別してパススルー (token はローテ開始時にサーバが発行、SHA-256 ハッシュを `mk_rotation_state` に保存して照合)
- **ローテーション中の読み取り API は制限しない**: GET 系 (例: `/api/v1/journals`) はオーバーラップ期間中も継続提供。クライアントは MK_old でアンラップ可能なまま (MK_old は新 wrapped_keys が追加されるだけで削除されていない、つまり完全に有効)。読み取りロックすると UX が不必要に劣化するため
- **タイムアウト自動 abort**: `auto_abort_at` を 7 日後に設定。経過後はサーバが自動 abort して rotation_state を NULL クリア (デバイス紛失等で commit/abort のいずれも飛んでこないケースの救済)。実行主体は **flask CLI コマンド `flask rotate-cleanup`** を cron / systemd timer で 1 時間ごとに起動する想定 (現行アプリは Celery / APScheduler を持たないため軽量に実装)

#### サイズ感

データ量が大きいと時間がかかる (E3 ベンチマーク次第)。バックグラウンド
ジョブ + 進捗バー UI が必要。E5 (監査) と E7 (移行) でも同じ仕組みを流用。

### 10.6 Q9 整理: REST API Bearer と鍵階層

| レイヤ | 認証 | 役割 |
|------|------|------|
| サーバアクセス | OAuthToken (Bearer) or session cookie | 「自分のデータにアクセスする権利」 |
| データ復号 | Master Key (クライアント保持) | 「データ内容を見る権利」 |

E2EE では **両方が必要**。Bearer だけでは暗号文しか取得できず内容は見えない。

client-py / client-tui:
1. Bearer token を `~/.config/iikanji/credentials` 等に保存
2. Master Key を OS のセキュアストレージ (keyring) に保存
3. 起動時に両方をロード → API 呼び出し + ローカル復号

Web クライアント:
1. Cookie (sessionid) でサーバアクセス
2. Master Key は Worker クロージャ + reload 毎に再アンラップ

### 10.7 Q10 整理: リロード時の再認証 UX

選択肢:

- **(a) 毎回 Passkey 再認証**: 最も安全だが UX 悪化
- **(b) sessionStorage に MK を保持**: XSS で全データ漏洩 → **禁止**
- **(c-old) ServiceWorker 内に MK を保持**: ServiceWorker はブラウザによって
  任意のタイミングで殺される (idle 中・メモリ圧迫時等) ため、状態保持には
  本質的に不向き → **棄却**
- **(c) SharedWorker で MK を保持**: SharedWorker は同一 origin の全タブで
  共有され、タブが 1 つでも生きていれば寿命が続く。リロード跨ぎ・タブ間
  共有とも自然に実現できる
- **(d) Page Visibility / idle 検知で自動再認証**: 一定時間操作なしで自動
  ロック (=SharedWorker から MK を消す)。(c) と組合せる
- **(e-rejected) IndexedDB に MK や wrapping key を永続化**: ブラウザクローズ後も
  保持できるが、XSS 1 件で全データ漏洩 → E2EE の前提を破るため **不採用**

→ **確定: (c) + (d) の組合せ**。SharedWorker 内 MK 保持 + 60 分 idle で
自動ロック。ブラウザを全タブ閉じれば MK は消滅 (再認証必要) — これは
平文鍵をディスク永続化しないための仕様。

#### 60 分タイムアウトの根拠

当初設計は 30 分だったが、実利用では誤ロックが頻発するため 60 分に拡大。
ロック解除はパスフレーズ入力 (Argon2id で遅い) より Passkey タップ (1 秒)
が遥かに快適なため、idle ロック自体が **パスフレーズ → Passkey 移行の
インセンティブ** として機能する設計意図がある。

公開後のユーザーログから idle 分布を観測し、必要なら 30〜90 分の範囲で
さらに調整する。

#### マルチタブ時の挙動

SharedWorker は同一 origin の全タブで共有されるため、タブ A でアンラップ
した MK はタブ B でも即座に利用可能。MK 状態変化 (`mkChanged` / `mkCleared`)
は全接続ポートに broadcast され、各タブが UI を同期する。

idle カウントの仕様:
- **タブ単位ではなく SharedWorker 単位で 1 つのカウンタ** を持つ
- いずれかのタブで Page Visibility が `visible` or ユーザー操作 (mouse/keyboard
  /touch/scroll) があれば SharedWorker に `touch` メッセージを送りカウンタ更新
- どのタブからもアクティビティがない状態が 60 分継続したら MK を消去
- 消去後はどのタブの操作も再認証要求

メインスレッド側の `touch` 発火は 1 分単位でスロットルし、SharedWorker への
postMessage が大量に流れないようにする。

#### 実装ファイル (E1 PR-G)

- `app/static/js/crypto/shared-worker-core.js` — `MasterKeyState` 純粋クラス
  (handle/checkIdle、Node テスト可能)
- `app/static/js/crypto/shared-worker.js` — SharedWorker ラッパー
  (onconnect、全ポート broadcast、setInterval idle 監視)
- `app/static/js/crypto/shared-client.js` — `SharedCryptoClient`
  (MessagePort 経由、`mkChanged`/`mkCleared` リスナー)
- `app/static/js/crypto/idle-monitor.js` — `IdleMonitor` (アクティビティ
  検知 + throttle 付き touch 発火)

### 10.8 監査アカウント (Lv1/Lv2/Lv3) と E1 鍵管理の関係

**結論**: §7 (d) 非同期ワークフロー方式により、**監査者は owner の MK にアクセスしない**。
E1 で `wrapped_keys` に監査用エントリを追加する必要は **ない**。

詳細:

- 監査者 (auditor) も通常ユーザーと同じく自分の MK / `wrapped_keys` を持つ
- owner が監査依頼を出すとき: 自分のクライアントで仕訳を MK 復号 → auditor の
  X25519 公開鍵で再暗号化 → AuditPackage としてサーバに保存
- auditor は自分の MK で自分の秘密鍵を unwrap → AuditPackage を復号
- **Lv3 (本人代理に近い) も同じフロー**で実現。「全仕訳スナップショット」を
  渡すだけで、リアルタイム代理閲覧は提供しない

これは「同時編集は本来の税務顧問のあり方ではない」(§7 (d) 推奨理由) という
v4.x → v5.0 の UX 変更の必然的帰結。`wrapped_keys.method='audit_grant'` 等の
追加は不要 (鍵共有を行わないため)。

実装上の追加は §7 で扱う `audit_packages` / `audit_responses` テーブル。
これは E1 ではなく **E5 (監査連携) のスコープ**。E1 は通常ユーザーの鍵管理
基盤のみに集中する。

### 10.9 E1 完了条件

- [ ] `wrapped_keys` マイグレーション (実装時の最新 revision の次番号)
- [ ] `users.mk_rotation_state` カラム追加 (同マイグレーション)
- [ ] `/api/v1/wrapped-keys` GET/POST/PUT/DELETE エンドポイント
  - DELETE は最終要素削除時に 409 Conflict
  - touch (`PUT /<id>/touch`) は認証必須 + 階層レート制限。**per-user を主軸**
    (例: 60 req/hour)、**per-IP は補助** (例: 5,000 req/hour、Tailscale / NAT 環境での
    バースト誤検知を避ける) という方針。具体値は本番運用ログを見て調整
- [ ] `/api/v1/wrapped-keys/rotate/begin` `.../commit` `.../abort` (ローテーション制御、rotation_token は SHA-256 ハッシュでサーバ保存)
- [ ] `flask rotate-cleanup` CLI コマンド (`auto_abort_at` 経過の自動 abort、1 時間ごと cron 起動想定)
- [ ] WebCrypto AES-GCM + Argon2id (hash-wasm) で MK wrap/unwrap が動作
- [ ] WebAuthn PRF 拡張で Passkey 経由の MK 派生が動作 (Q2 結果次第で fallback 設計)
- [ ] BIP-39 24 単語のリカバリシード生成 + 表示 + 入力時チェックサム検証 UI (HKDF で derived_key 派生)
- [ ] MK ローテーション (空データでフロー検証、`mk_rotation_state` を含む再開可能設計、E3 で本番投入)
- [ ] 既存 webauthn_credentials との DB レベル CASCADE 連動 (FK to `webauthn_credentials.id`)
- [ ] Q9/Q10 の実装 (Bearer + MK の併用、SharedWorker による MK 永続化、マルチタブ idle カウンタ共有)

E2 (API キー E2EE 化) はこの基盤を最初に使う「最小スコープ検証」。

---

## 11. E2 設計スケッチ (API キー E2EE 化 — 最小スコープ検証)

E1 で構築した鍵管理基盤の **最初の実応用**。`UserAIConfig.api_key_encrypted`
(現状 Fernet サーバ暗号化) を **クライアント暗号化** に移行する。データ量が
小さく (1 ユーザー数行)、影響範囲が限定的なため、E3 (仕訳暗号化) の前に
パターンを確立する位置づけ。

### 11.1 現状 (v4.x) の構造

```python
# app/models/ai_config.py
class UserAIConfig(db.Model):
    api_key_encrypted = db.Column(db.LargeBinary, nullable=False)
    # ↑ Fernet 暗号化された API キーバイト列

# app/services/ai_receipt.py
def _get_fernet():
    """SECRET_KEY から Fernet インスタンスを生成"""

def decrypt_api_key(encrypted_bytes):
    return _get_fernet().decrypt(encrypted_bytes)

# ai_receipt.py:723
raw_key = decrypt_api_key(config.api_key_encrypted)
# ↑ サーバが復号できる = サーバ侵害で全ユーザーの API キーが漏洩
```

問題点:
- サーバが `SECRET_KEY` を持っているので Fernet 暗号化は実質「暗号化された状態
  でストレージに置く」程度の効果しかない (サーバ侵害時の保護が弱い)
- E2EE の脅威モデル §1 で挙げた「サーバ内部犯」「サーバ侵害」を防げない

### 11.2 目標 (v5.0)

```python
# app/models/ai_config.py (E2 後)
class UserAIConfig(db.Model):
    # 旧: api_key_encrypted (LargeBinary)
    api_key_blob = db.Column(db.LargeBinary, nullable=False)  # 暗号文 + タグ
    api_key_iv   = db.Column(db.LargeBinary, nullable=False)  # AES-GCM IV (12B)
    # サーバは復号できない。クライアントが MK で復号
```

サーバの責務:
- 暗号文を保管するだけ
- 復号は一切行わない (`decrypt_api_key` 関数は **削除**)
- AI 呼び出しは v5.0 で **クライアント側に移行**:
  - Web: サーバサイドルート (現行 `ai_receipt.py`) が使えない (E2EE 前提でサーバ
    が平文を扱えない) ので、ブラウザの fetch で OpenAI/Anthropic API を直接呼ぶ
  - `client-py` / `client-tui`: 元々クライアント側で動くので問題なし

### 11.3 サーバ側 AI 呼び出しの廃止

現行のサーバ側 AI 呼び出し (ai_receipt.py の各 provider handler) は v5.0 で
**全廃** する。理由:

1. サーバが API キーを復号できない (E2EE の前提)
2. サーバが画像を復号できない (E4 後)
3. サーバが LLM プロンプトを組み立てる材料が暗号化される

クライアント側で完結する形に移行:

```
[v4.x]
client → upload image → server → decrypt → call OpenAI → return suggestion → server → store ai_draft
                                  ↑ サーバが平文を扱う

[v5.0]
client → encrypt image → upload → server (暗号文ストレージのみ)
client → fetch image → decrypt → call OpenAI directly → encrypt suggestion → upload as ai_draft
       ↑ クライアントが直接 LLM を呼ぶ。サーバは一切平文を見ない
```

UX 影響:
- ブラウザ閉じてる間に AI 解析を進める「バックグラウンド解析」は実現困難
  (= クライアントが起動していないと AI 解析できない)
- 自家ホスト LLM (`llama_cpp` provider) は E2EE と両立不可なので v5.0 で廃止
  (確定事項表)
- BYOK (ユーザー自身の API キー) のみサポート

### 11.4 マイグレーション戦略

`user_ai_configs` テーブルに新カラム追加 + 旧カラム削除を 2 段階で:

```
Phase E2-a (互換期間):
  1. ALTER TABLE user_ai_configs
     ADD COLUMN api_key_blob bytea NULL,
     ADD COLUMN api_key_iv   bytea NULL;
  2. 既存ユーザー: ログイン時にクライアントで以下を実行
     - サーバから api_key_encrypted を取得 (一時的にサーバが復号して平文返却)
     - クライアントが MK で再暗号化 → api_key_blob/iv を PUT
     - サーバで api_key_encrypted = NULL に更新
  3. 全ユーザーの api_key_encrypted = NULL になったら次フェーズ

Phase E2-b (旧カラム削除):
  4. ALTER TABLE user_ai_configs
     ALTER COLUMN api_key_blob SET NOT NULL,
     ALTER COLUMN api_key_iv   SET NOT NULL,
     DROP COLUMN api_key_encrypted;
```

§6 の一斉移行とは異なり、E2 だけは **段階的移行が可能**。データ量が小さく、
ユーザーごとに独立しているため。

⚠️ **Phase E2-a の一時平文返却はサーバが MK を知らない状況下では不可能**。
正しいフロー:

```
クライアント (移行ユーザー):
  1. 旧 api_key_encrypted を GET (サーバが Fernet で復号して平文返す)
     → これは互換 endpoint `/api/v1/ai-config/migrate-key` (E2 限定)
  2. クライアントが MK で再暗号化 → PUT /api/v1/ai-config (api_key_blob/iv)
  3. サーバが旧カラムを NULL クリア
  4. 全件移行完了したら互換 endpoint も廃止 + 旧カラム DROP

セキュリティ: 移行期間中、サーバ生成の SECRET_KEY による復号能力が残る。
全ユーザー移行完了後に SECRET_KEY を rotate して残存リスクを下げる。
```

#### migrate-key endpoint のセキュリティ要件

`/api/v1/ai-config/migrate-key` は本来 E2EE の前提を破る "サーバ側復号" を
許容する **移行限定の例外 endpoint** であり、悪用されると全ユーザーの API
キーが取得可能になる。以下を実装必須:

- `@login_required` (current_user 以外の鍵は返さない)
- レート制限: **per-user 1 回限り** (`UserAIConfig.migrated_at` 等で再呼出しを拒否)
- 呼出成功後は即時 `api_key_encrypted = NULL` にしてサーバ側鍵材料を消去
- 全件 NULL 確認用の CLI: `flask ai-config migration-status` (未移行件数を集計)
- 全ユーザー移行完了確認後、`flask ai-config drop-migrate-key` 等で route 削除
  + マイグレーション (旧カラム DROP) を実行

### 11.5 API 変更

```
変更前 (v4.x):
  GET    /api/v1/ai-config        → 復号せず metadata のみ返却
  POST   /api/v1/ai-config        → 平文 api_key を受け取り Fernet 暗号化して保存
  DELETE /api/v1/ai-config        → 削除

変更後 (v5.0):
  GET    /api/v1/ai-config        → api_key_blob + api_key_iv をそのまま返却 (暗号文)
  POST   /api/v1/ai-config        → api_key_blob + api_key_iv を受け取って保存
                                    (サーバは復号しない)
  DELETE /api/v1/ai-config        → 削除
  POST   /api/v1/ai-config/migrate-key (E2-a 限定、移行完了後に廃止)
                                  → 旧 api_key_encrypted を返却 (Fernet 復号済)
```

### 11.6 クライアント実装の変更

```
v4.x: 設定画面
  ユーザーが API キーを入力 → POST /api/v1/ai-config (平文)
                              ↑ HTTPS で守られるがサーバが平文を見る

v5.0: 設定画面
  ユーザーが API キーを入力 → クライアントが MK で AES-GCM 暗号化
                              → POST /api/v1/ai-config (暗号文 + IV)
                              ↑ サーバは復号できない
```

ai_receipt.py 利用箇所:
- Web: 廃止 (クライアント側で直接 LLM を呼ぶ)
- client-py / client-tui: API キーをローカル復号 → 直接 LLM 呼び出し

### 11.7 E2 完了条件

- [ ] `user_ai_configs` マイグレーション (旧 `api_key_encrypted` 削除、新 `api_key_blob` / `api_key_iv` 追加、2 段階)
- [ ] `/api/v1/ai-config` GET / POST / DELETE (暗号文受け渡しのみ)
- [ ] `/api/v1/ai-config/migrate-key` (互換 endpoint、per-user 1 回限り、呼出成功後に旧カラム NULL クリア、移行完了後に削除)
- [ ] `flask ai-config migration-status` / `drop-migrate-key` CLI (移行進捗確認 + 完了後 route 削除)
- [ ] クライアント側 (Web) で MK を使った AES-GCM 暗号化/復号
- [ ] AI 呼び出しのクライアントサイド移行 (ai_receipt.py 廃止候補の整理)
- [ ] `client-py` / `client-tui` の `/api/v1/ai-config` I/F 更新 (POST が平文 → 暗号文に変わるため)
- [ ] サーバ側 `decrypt_api_key` / `_get_fernet` 削除 (移行完了後)
- [ ] `SECRET_KEY` のローテーション (移行完了後の残存リスク低減)
- [ ] テスト: 旧 Fernet 暗号化済データから新形式への移行が成功する
- [ ] テスト: サーバが api_key_blob を復号できないことの確認 (= Fernet で復号試行が失敗)
- [ ] テスト: migrate-key endpoint の per-user 1 回限り制約

### 11.8 E3 への影響

E2 で確立されるパターン:

- クライアント暗号化されたバイト列を BLOB として保管
- IV を別カラムに保管
- マイグレーションは段階的に (旧カラム残しつつ新カラム導入 → 移行完了で旧削除)
- サーバ側の復号関数は完全削除

これを `journal_entries` / `journal_entry_lines` / `medical_expenses` 等の
大規模テーブルへ適用するのが E3。E2 のテストカバレッジが E3 設計の基礎になる。

---

## 12. E3 設計スケッチ (仕訳データの暗号化 — 本丸)

E2EE 移行の中核。`JournalEntry` / `JournalEntryLine` / `MedicalExpense` /
`BalanceCache` を暗号化 BLOB に変換し、レポート集計と検索のクライアントサイド化を行う。

### 12.1 暗号化対象テーブル

```
journal_entries:
  暗号化対象: date, description, source, batch_id, fiscal_period
  非暗号化:   id, user_id, entry_number (※ 採番のため平文)
              fiscal_year (※ 年度フィルタのため新規・平文、date 暗号化に伴う代替)
              created_at, updated_at, encrypted_blob, blob_iv (新規)

journal_entry_lines:
  暗号化対象: account_code, debit_amount, credit_amount, description
  非暗号化:   id, journal_entry_id, line_order (※ ソートのため)
              created_at, updated_at, account_user_id (テナント分離 FK 維持)
              encrypted_blob, blob_iv (新規)

medical_expenses:
  暗号化対象: 全フィールド (patient_name, hospital_name, amount_paid 等)
  非暗号化:   id, user_id, year (税年度集計のため平文)
              encrypted_blob, blob_iv (新規)

balance_caches:
  → **テーブル削除** (E2EE 下ではサーバが残高計算できない)
  → クライアント側で IndexedDB に暗号化済キャッシュを保持
```

⚠️ **年度フィルタ用に `journal_entries.fiscal_year smallint` を新設** (平文)。
`date` が暗号化されてサーバから見えなくなるため、`GET /api/v1/journals?year=2026`
のようなサーバサイドフィルタは `fiscal_year` カラムで実現する。クライアントが
date から年度を計算して書き込み時に同時に送る。漏れる情報は「何年度の仕訳が
何件あるか」のみで、内容は守られる。

各テーブルに `encrypted_blob (bytea)` + `blob_iv (bytea 12B)` を追加し、
**1 レコード = 1 JSON 暗号文** とする。フィールド単位ではなくレコード単位の
暗号化により、IV 管理が単純化される。

### 12.2 暗号化レコード形式 (JSON-then-encrypt)

```json
// JournalEntry の plaintext (シリアライズ前)
{
  "v": 1,
  "date": "2026-05-22",
  "description": "スーパーで食材購入",
  "source": "cashbook",
  "batch_id": null,
  "fiscal_period": 5
}

// 暗号化フロー
plaintext_bytes = utf8(JSON.stringify(plaintext))
iv              = crypto.getRandomValues(new Uint8Array(12))
ciphertext      = AES-GCM(MK, iv, plaintext_bytes, aad)
encrypted_blob  = ciphertext + 16B tag (cryptography ライブラリ既定)
```

#### AAD フォーマット (全テーブル共通仕様)

AAD は **テーブル種別 + 識別 ID の連結バイト列**。すべての整数フィールドは
**big-endian 固定長エンコード**。サーバが BLOB を別ユーザー / 別行に
すり替えても復号失敗で検出される。

エンコード仕様:
- 整数 ID (user_id, entry_id, line_id, expense_id): **uint64 big-endian (8B)**
- 年 / 期 (year, period): **uint16 big-endian (2B)** (smallint カラムに対応)
- セパレータ `\0`: 可読性のため挿入 (固定長なので曖昧性排除には不要だが、
  ダンプ時の境界を見やすくする)

```
journal_entries:
  aad = b"je\0" + uint64_be(user_id) + b"\0" + uint64_be(entry_id)

journal_entry_lines:
  aad = b"jel\0" + uint64_be(user_id) + b"\0" + uint64_be(journal_entry_id)
                + b"\0" + uint64_be(line_id)
  ※ line_id 単独だと他ユーザーの同 line_id にすり替え可能なので user_id + journal_entry_id も必須

medical_expenses:
  aad = b"me\0" + uint64_be(user_id) + b"\0" + uint64_be(expense_id)

balance_cache_blobs:
  aad = b"bcb\0" + uint64_be(user_id) + b"\0" + uint16_be(year) + b"\0" + uint16_be(period)
```

- テーブル種別プレフィックス (`je` / `jel` / `me` / `bcb`) で「JournalEntry の
  暗号文を MedicalExpense 行に入れる」攻撃も検知
- big-endian 固定長エンコードにより Web / Python / TUI クライアント間で完全互換
- `v` (version) フィールドで将来のスキーマ変更に対応

### 12.3 レポート集計のクライアントサイド化

現行 (v4.x) はサーバ側でレポート集計:

```python
# app/services/tax.py 等
trial_balance = db.session.query(
    func.sum(JournalEntryLine.debit_amount),
    func.sum(JournalEntryLine.credit_amount),
).group_by(JournalEntryLine.account_code).all()
# ↑ サーバが平文 (account_code, debit_amount, credit_amount) を見て集計
```

E2EE 後はサーバが暗号文しか見られないので、**クライアント側で全件取得 → 復号 → JS / Python で集計**:

```javascript
// Web クライアント (集計サービスは E3 完了条件で新設)
const lines = await fetchAllJournalEntryLines();  // 暗号文を全件 GET
const plaintexts = await Promise.all(
  lines.map(l => crypto.decrypt(l.encrypted_blob, l.blob_iv, aad=...))
);
const trialBalance = aggregate(plaintexts);  // クライアントで集計
```

#### 影響を受けるレポート

| レポート | 現行サーバ集計 | E2EE 後 |
|---|---|---|
| 試算表 | `app/services/balance.py` | クライアント側 (`/static/js/reports.js`) |
| 損益計算書 | `app/views/reports.py` | クライアント側 |
| 貸借対照表 | `app/views/reports.py` | クライアント側 |
| 月次比較 | `app/services/tax.py` | クライアント側 |
| 確定申告集計 | `app/services/tax.py` | クライアント側 |
| 元帳 | `app/views/reports.py` | クライアント側 (年度全件取得 + フィルタ) |
| 医療費控除 | `app/services/medical.py` | クライアント側 |

#### パフォーマンス影響 (Q3)

- 仕訳行数 1 万件 → 復号約 20ms (AES-256-GCM, 開発機 538K ops/s 基準)
- 仕訳行数 10 万件 → 復号約 200ms (実用範囲)
- 仕訳行数 100 万件 → 復号約 2秒 (E3 で実測、必要なら年度別シャーディング)
- IndexedDB キャッシュで「初回ロード時のみ全件復号、以後は差分のみ」戦略

### 12.4 検索の代替手段

サーバ側で `date BETWEEN ... AND ...` や `description LIKE '%...%'` ができなくなる。

選択肢:

- **(a) クライアント全件取得 + JS フィルタ** (推奨): 検索性は犠牲、E2EE 純度高い
- **(b) クライアント側全文インデックス** (IndexedDB に検索用 token を保管):
  実装複雑、暗号化との両立が課題
- **(c) サーバ側 blind index** (HMAC ベース): 部分検索不可、決定論的なので
  パターン推測されうる

→ **暫定: (a) を採用**。検索 UI は「年度フィルタ + 全件取得 + JS で絞り込み」。
年度別にデータを分けて取得することで 1 リクエストあたりの転送量を抑える。

### 12.5 CSV / OFX / Web 貼り付けの E2EE 対応

現行はサーバ側で CSV をパースして仕訳を生成:

```
v4.x:
  client → upload CSV → server (parse) → preview → confirm → INSERT 仕訳

v5.0:
  client → parse CSV locally → preview → confirm
         → client が仕訳を暗号化 → POST /api/v1/journals (暗号文一括)
```

サーバ側の `csv_import.py` / `ofx_import.py` は **クライアント側へ移行**:
- Web: JS で CSV / OFX をパース (PapaParse 等、~20KB)
- `client-py` / `client-tui`: Python で完結 (既存ロジック流用)

Web 貼り付け (Web Import) も同様にクライアント側パース化。

### 12.6 AI 証憑仕訳 (AIDraft) の E2EE 対応

§11.3 の方針 (サーバ側 AI 呼び出し全廃) と整合:

- 証憑画像は E4 でクライアント暗号化済みストレージへ
- AI 解析はクライアント側で直接 LLM (OpenAI/Anthropic) を fetch
- `AIDraft` テーブルは「下書きの暗号化スナップショット」のみ保管 (フル E2EE)
- `discord_webhook_url` 等の外部通知は v5.0 で廃止 (§4 Webhook 非両立)

#### Webhook 廃止のユーザー影響

v4.x の Discord 通知 (`AIDraft.discord_webhook_url` / `WebhookConfig`) を
利用しているユーザーは v5.0 アップグレード時に通知が止まる。リリースノート
への明記が必須:

- v5.0 リリース時の移行ガイドに「Webhook 通知は廃止。仕訳登録の確認は
  クライアント常駐 + Page Visibility イベントで代替してください」
- 既存 `WebhookConfig` レコードは移行マイグレーションで削除 (E7 で実行)
- ユーザーが事前にエクスポートしたい場合のオプトアウト手段は不要 (Webhook URL
  自体はユーザーが把握済み)

### 12.7 月次確定 (FiscalClose) の扱い

`FiscalClose` テーブルは:
- `user_id, year, closed_period` の連番管理 → **平文維持** (フィルタに必要)
- 確定済み判定はサーバ側で続行 (write API でロック判定)
- 暗号化された仕訳の改ざんは AAD + タグ検証で検出されるので、`closed_period`
  のみで write 拒否すれば整合性は保てる

### 12.8 BalanceCache の廃止と代替

現行 `BalanceCache` テーブル (確定済み残高キャッシュ) は **削除**:

```
v4.x:
  fiscal_close 確定時に balance_caches に累計借方/貸方を INSERT
  サーバが集計クエリで利用

v5.0:
  サーバが残高を計算できない → balance_caches は無意味
  クライアント側の IndexedDB に暗号化済みキャッシュを保持
  - キー: (year, period, account_code)
  - 値: 暗号化された accumulated_debit / accumulated_credit
  fiscal_close 確定時にクライアントがキャッシュを生成 → サーバに POST
                                                       (encrypted_blob として)
```

新テーブル `balance_cache_blobs` (例):

```
| カラム            | 型           | 用途 |
|------------------|--------------|------|
| id               | bigserial    | PK |
| user_id          | bigint       | テナント分離 |
| year             | smallint     | 平文 (フィルタ用) |
| period           | smallint     | 平文 (フィルタ用) |
| encrypted_blob   | bytea        | 全 account_code の残高を 1 JSON にまとめて暗号化 |
| blob_iv          | bytea (12B)  | |

UNIQUE (user_id, year, period)
```

サーバはストレージとしてのみ機能、クライアントが復号して使用。

### 12.9 マイグレーション戦略 (§6 と連動)

§6 で確定済の「一斉移行」を実行する (`§6 移行戦略 (一斉移行)` 参照)。E3 が
最大の移行コスト。

**一時 MK のライフサイクル (§6 から要約)**:
- メンテナンスウィンドウ開始時にサーバが一時 MK を生成
- 全データをサーバ側で一時 MK で暗号化 (この時点で旧平文カラムは
  encrypted_blob にも入る)
- 各ユーザーが Passkey / パスフレーズ設定完了後、クライアントが「一時 MK で
  復号 → 自分の MK で再暗号化」を実行
- 全ユーザー再暗号化完了後、サーバから一時 MK を破棄

```
Phase E3 マイグレーション:
  1. 全テーブルに encrypted_blob / blob_iv カラムを ADD (NULL)
  2. メンテナンスウィンドウで全データを暗号化変換 (§6 のサーバ生成鍵フェーズ)
  3. クライアント鍵設定完了後、再暗号化 (§6 続き)
  4. 全件 encrypted_blob が NOT NULL になったら旧平文カラムを DROP
```

旧平文カラム DROP までの猶予期間:
- §6 の「猶予期間 30 日」と整合
- 期間中、クライアントは旧平文 or 新暗号文のどちらでも読める「dual read」
- DROP 後は新暗号文のみ

#### dual read 期間中の API 契約

`GET /api/v1/journals` (および類似 endpoint) のレスポンスは
**常に新形式 (encrypted_blob + blob_iv)** で返す。サーバ側で以下のロジック:

```
未移行行 (encrypted_blob IS NULL):
  → サーバが「一時 MK」(§6 サーバ生成鍵フェーズの残り鍵) で旧カラムを暗号化
    オンザフライで encrypted_blob + blob_iv を生成して返す
  → クライアントはサーバ生成鍵で復号 (移行ウィザード中のみ可能)

移行済行 (encrypted_blob IS NOT NULL):
  → そのまま返す
  → クライアントは自分の MK で復号
```

`POST /api/v1/journals` (新規仕訳作成) は**移行期間中も暗号文のみ受付**。
クライアントが平文 POST するルートは v5.0 で廃止。これにより新規データは
即時 E2EE 化される。

dual read 期間後 (旧カラム DROP 後): サーバ側の一時 MK は破棄、`encrypted_blob`
がない行は存在しなくなる。

### 12.10 E3 完了条件

- [ ] journal_entries / journal_entry_lines / medical_expenses にカラム追加マイグレーション (encrypted_blob, blob_iv)
- [ ] balance_cache_blobs テーブル新設 + 旧 balance_caches DROP
- [ ] AAD (user_id + entry_id) による暗号文すり替え検知の実装
- [ ] レポート計算のクライアントサイド化 (試算表 / P/L / B/S / 月次比較 / 確定申告集計 / 元帳 / 医療費控除)
- [ ] CSV / OFX / Web 貼り付けのクライアントサイドパース化 (Web: JS、`client-py` / `client-tui`: 既存 Python ロジック流用 + 仕訳暗号化を追加)
- [ ] AI 証憑仕訳 (AIDraft) のクライアントサイド AI 呼び出し統合
- [ ] サーバ側 `tax.py` / `balance.py` / `csv_import.py` / `ofx_import.py` 等の廃止
- [ ] IndexedDB キャッシュ戦略 (年度別、差分更新)
- [ ] パフォーマンス目標: 仕訳 10 万件で初回ロード 5 秒以内
- [ ] `journal_entries.fiscal_year smallint` (平文) カラム追加 + 既存データ移行
- [ ] **クライアント側書き込みバリデーション**: debit / credit 合計一致、`fiscal_period=16` の手動入力禁止 (サーバ側バリデーションは fiscal_period 暗号化で消えるため)
- [ ] dual read 期間中の API: `GET` は常に encrypted_blob 形式で返却 (未移行行はサーバ生成鍵で on-the-fly 暗号化)、`POST` は暗号文のみ受付
- [ ] テスト: AAD 攻撃 (他ユーザーの暗号文を自分の行にすり替え) で復号失敗
- [ ] テスト: テーブル種別プレフィックス付き AAD で異テーブル暗号文の混入を検知
- [ ] テスト: 月次確定後の write 拒否がクライアント書き込みでも効く
- [ ] テスト: クライアント側 debit/credit 合計一致バリデーションが効く
- [ ] テスト: dual read 期間中の旧データ取得が成功する (移行前後の互換)
- [ ] E2 で確立したパターン (BLOB + IV カラム、互換 endpoint) を E3 でも踏襲
- [ ] `client-py` / `client-tui` の仕訳 CRUD API クライアントを E3 形式に更新
- [ ] WebhookConfig / AIDraft.discord_webhook_url の v5.0 廃止 (E7 マイグレーションで削除、リリースノートに移行ガイド記載)

### 12.11 信頼境界の変化 (サーバサイドバリデーション喪失)

E3 後、複式簿記の整合性検証はサーバから消える。具体的に失われる検証:

| 現行サーババリデーション | E2EE 後 | 代替 |
|---|---|---|
| `debit_amount + credit_amount` 合計一致 | サーバから見えず | クライアント側 + 監査時の整合性検査 |
| `fiscal_period=16` (損益振替) の手動入力禁止 | サーバから見えず | クライアント側 |
| `account_code` が `accounts` テーブルに存在するか | サーバから見えず | クライアント側 (`account_user_id` FK のみサーバが検証) |
| `fiscal_period` の月次確定整合性 | `closed_period` 平文維持で部分的に継続 | サーバ書き込み拒否 + クライアント警告 |

#### 影響と緩和策

1. **悪意あるクライアント (自前実装で改ざん)**:
   - 不正な仕訳 (合計不一致、`fiscal_period=16` 手動入力) を POST 可能
   - **本人のデータが壊れるだけで他ユーザーへの影響なし** (AAD で隔離)
   - クライアントが自分で自分のデータを壊しても規約上免責 (リカバリシード等
     と同じく「自己責任の自由」)
2. **バグのあるクライアント (公式 Web / client-py / client-tui のバグ)**:
   - 同様にデータ破損リスク → リリース前の自動テスト網羅で予防
   - 修復可能性のために「ユーザーが自分の MK でローカル整合性チェック → 修復
     提案」を offer する管理 UI を E6 で検討
3. **監査アカウント (Lv2/Lv3) からの不正書き込み**:
   - §7 (d) ワークフロー方式により監査者は MK を持たない → そもそも書き込み
     不可能。修正案を owner に返すだけ。owner が反映するか拒否するかは owner
     の意思で決まる
4. **クライアント側バリデーションのテスト**:
   - 公式 Web / client-py / client-tui は同じバリデーションロジックを共有
     (将来は `iikanji-validation` 共通ライブラリ等に切り出し検討)

#### 受容するリスク

- 「サーバが複式簿記の整合性を強制できない」ことは E2EE の本質的トレードオフ
- 帳簿の整合性 = ユーザーが信頼するクライアントの正しさ + 自分の操作の正しさ
- 整合性違反は事後的にクライアント側監査で検出可能 (年次決算前のチェックを
  推奨)

### 12.12 平文で漏れる情報 (脅威モデル §1 補完)

E3 後にサーバから可視な仕訳メタデータ:

- 仕訳の **件数** (entry_id 連番から推測可能)
- 仕訳の **作成時刻 / 更新時刻** (created_at / updated_at 平文)
- 仕訳の **年度別件数** (`journal_entries.fiscal_year` 平文)
- 仕訳明細の **本数の分布** (1 仕訳あたり何本の `journal_entry_lines` があるか)
- **使用科目の数** (`account_user_id` 平文 FK から、ユーザーが何個の科目を
  運用しているかが分かる)
- AI 証憑画像のサイズ・件数 (E4 で対応、現状は §4 「暗号文のサイズ」として
  許容)

これらは脅威モデル §1 「守らないもの」と整合。事業規模・使用頻度の推測は可能
だが、内容 (金額・取引先・科目名) は保護される。

### 12.13 `balance_cache_blobs` のペイロードサイズと部分更新コスト

§12.8 で「全 account_code の残高を 1 JSON にまとめて暗号化」する設計は、
月次確定時に **一部の account_code 変更でも全 BLOB を再生成・再アップロード**
する必要がある。意図的なトレードオフ:

- **採用理由**: 1 (year, period) ペアあたり 1 BLOB に集約することで、API
  呼び出し数とサーバストレージのインデックスサイズを最小化
- **想定ペイロード**: 個人事業主の標準科目 ~100 個 × 1 行 ~50B = 5KB 程度。
  暗号化後でも 5-6KB。問題なし
- **法人や大規模ユーザー**: 科目数が 1000+ の場合は 50KB 程度。それでも 1
  リクエストで完結する範囲
- **代替案 (科目別分割)**: 採用しない。インデックスが肥大化し、ローテーション
  時の処理対象数が増える

### 12.14 PapaParse 等のクライアントライブラリ

§12.5 で言及した CSV パーサ等の管理方針:

- **CDN 経由 + SRI 必須**: jsdelivr で `<script integrity="sha384-...">` 形式
- **バージョン固定**: メジャー固定 + マイナーパッチは手動更新で評価
- 候補: PapaParse 5.x (MIT, ~20KB minified) / csv-parse (MIT, ~30KB)
- 実装フェーズで SBOM (Software Bill of Materials) に追加して脆弱性監視に組込

### 12.15 E4 (証憑画像) への影響

E3 で確立されるパターン:

- JSON-then-encrypt (レコード単位の暗号化)
- AAD で「他ユーザーの暗号文すり替え」攻撃を防ぐ
- IndexedDB クライアントキャッシュ
- レポート集計の完全クライアントサイド化

E4 では同じパターンを画像 BLOB に適用:
- 画像本体 = AES-GCM(MK, iv, image_bytes)
- AAD = user_id + voucher_id
- サムネイル生成もクライアント側

---

## 13. E4 設計スケッチ (証憑画像の E2EE 化)

E3 の JSON-then-encrypt パターンを **画像 BLOB に適用**。電帳法の改ざん防止
要件と E2EE をどう両立させるかが核心。

### 13.1 暗号化対象

```
vouchers:
  暗号化対象: original_filename, image_mime (encrypted_meta_blob にまとめて格納)
  非暗号化:   id, user_id, journal_entry_id (FK 維持)
              image_key (ストレージパス、平文)
              thumbnail_key (サムネイルのストレージパス、平文)
              encrypted_meta_blob, meta_iv (新規、original_filename + image_mime 等のメタ情報)
              uploaded_at (時刻情報は脅威モデル §1「漏れて構わない情報」)
              file_hash_plain (クライアント計算) / file_hash_cipher (サーバ計算)
  画像本体:   ストレージ (Local / S3) に暗号化済バイト列として保存
              ファイル名は image_key (UUID 等)、復号鍵は MK

voucher_audit_logs:
  暗号化対象: detail (JSON)
  非暗号化:   id, voucher_id, user_id, action, created_at (フィルタ用途)
              encrypted_detail_blob, detail_iv (新規)
```

### 13.2 画像本体の暗号化

```
クライアント (アップロード):
  1. ユーザーが画像選択 (File API)
  2. image_bytes = await file.arrayBuffer()
  3. iv = crypto.getRandomValues(new Uint8Array(12))
  4. aad = b"vimg\0" + uint64_be(user_id) + b"\0" + uint64_be(voucher_id)
  5. ciphertext = AES-GCM(MK, iv, image_bytes, aad)
  6. POST /api/v1/vouchers (multipart):
     - file: ciphertext blob (元画像と同じ extension 不可。`.bin` 等)
     - iv: hex string
     - mime_blob: 暗号化された image/jpeg 等の文字列
     - original_filename_blob: 暗号化された元ファイル名
  7. サーバ: ストレージに ciphertext を保存、image_key を返却

クライアント (閲覧):
  1. GET /api/v1/vouchers/<id> → encrypted_meta_blob, image_key
  2. GET /api/v1/vouchers/<id>/image (or presigned URL) → ciphertext
  3. クライアントで AES-GCM 復号 (aad = b"vimg\0" + ...)
  4. <img src="blob:..."> で表示
```

サーバはバイト列しか触れない。画像の中身、ファイル名、MIME タイプすべて
クライアントが復号後に得る。

#### AAD 一覧 (vouchers 全暗号化フィールド)

```
画像本体:
  aad = b"vimg\0"   + uint64_be(user_id) + b"\0" + uint64_be(voucher_id)

サムネイル:
  aad = b"vthumb\0" + uint64_be(user_id) + b"\0" + uint64_be(voucher_id)

encrypted_meta_blob (original_filename + image_mime 等):
  aad = b"vmeta\0"  + uint64_be(user_id) + b"\0" + uint64_be(voucher_id)

voucher_audit_logs.encrypted_detail_blob:
  aad = b"valog\0"  + uint64_be(user_id) + b"\0" + uint64_be(voucher_audit_log_id)
```

§12.2 のテーブル種別プレフィックスパターンを踏襲。テーブル間 / フィールド間
のすり替え攻撃を全部検知。

**AAD 重要**: `voucher_id` は POST 時にサーバが採番するため、**クライアントは
2 段階で upload する必要がある**:

```
Step 1: POST /api/v1/vouchers/init → voucher_id 採番のみ (空レコード作成)
Step 2: 採番された voucher_id を AAD に含めて暗号化 → PUT /api/v1/vouchers/<id> で
        実体 upload
```

または、サーバが採番時にプレースホルダ (UUID) を返し、それを `voucher_id` の
代わりに AAD に使う方式も検討。E4 実装時に確定。

### 13.3 サムネイル生成のクライアントサイド化

現行 (v4.x) はサーバ側で Pillow を使ってサムネイル生成。E2EE 後はサーバが
原画像を見られない:

```
クライアント (アップロード時):
  1. <canvas> で original_image を 200x200 にリサイズ
  2. canvas.toBlob() で thumbnail_bytes 取得
  3. AES-GCM 暗号化 (aad = b"vthumb\0" + uint64_be(user_id) + b"\0" + uint64_be(voucher_id))
  4. POST /api/v1/vouchers/<id>/thumbnail (同様に暗号文を upload)
```

`Voucher` テーブルに `thumbnail_key` (Local / S3 上のパス) を追加。サムネイル
表示時もクライアント復号。

代替案 (見送り): サーバ側で「暗号文 → サムネイル生成」は不可能 (画像本体を
見られない)。

### 13.4 電帳法 `file_hash` の扱い (Q11)

**Q11**: 電帳法スキャナ保存の改ざん防止証跡 (`Voucher.file_hash`、現行は
サーバが SHA-256 計算) を E2EE 下でどう扱うか。

選択肢:

- **(a) 暗号文の SHA-256 を保存**: サーバが計算可能だが、暗号文の不変性しか
  証明しない (鍵紛失 = 検証不能)
- **(b) 平文の SHA-256 をクライアントが計算してアップロード**: サーバは
  クライアントを信頼する必要があるが、電帳法上の「改ざん防止」は維持
- **(c) HMAC(MK, plaintext)**: タンパー検出に MK が必要 (= ユーザー自身)、
  暗号学的に最も堅い

→ **(b) + (a) のハイブリッド**:
- `file_hash_plain` (= SHA-256(plaintext), 平文ハッシュ): サーバに保存。
  クライアントが復号後に再計算して検証
- `file_hash_cipher` (= SHA-256(ciphertext), 暗号文ハッシュ): サーバが
  upload 時に計算して保存。サーバ管理者が「あるはずの画像が改ざんされて
  いないか」を MK なしで検証可能
- 両方が `Voucher` テーブルにあれば、改ざん検出は二重化される

データモデル:

```
vouchers:
  file_hash_plain  bytea  -- SHA-256(plaintext)、クライアント送信
  file_hash_cipher bytea  -- SHA-256(ciphertext)、サーバ計算 (v4.x の file_hash 相当)
```

#### 電帳法対応の継続性

- 「訂正削除の事実と内容を確認できること」: `VoucherAuditLog` 継続 (detail は
  暗号化、action は平文でフィルタ可能)
- 「タイムスタンプ」: `uploaded_at` 平文継続
- 「ハッシュ検証」: `file_hash_plain` + `file_hash_cipher` 二重化
- 「検索機能 (日付 / 金額 / 取引先)」: §13.5 参照

### 13.5 検索 (Voucher search 電帳法対応) の影響

現行 `Voucher` 検索は仕訳の `date / amount / description` 経由 (Phase 2)。
E3 後はこれらが暗号化されるので、§12.4 の「クライアント全件取得 + JS フィルタ」
を踏襲。

- 日付フィルタ: `journal_entries.fiscal_year` (平文) で年度別取得 → クライ
  アント側で日付フィルタ
- 金額フィルタ: クライアント側でレンジ計算
- 摘要 / 取引先検索: クライアント側で部分一致

### 13.6 AI 証憑解析の連携 (§11.3, §12.6 と整合)

```
クライアント (AI 解析):
  1. 暗号文画像を fetch + AES-GCM 復号 → 平文 image_bytes
  2. base64(image_bytes) を OpenAI/Anthropic API に直接送信
  3. レスポンス (仕訳候補 JSON) を MK で暗号化
  4. POST /api/v1/ai-drafts (encrypted_blob として)
```

サーバ側 AI 呼び出し (`ai_receipt.py`) は v5.0 で全廃 (§11.3 / §12.6 と一致)。

### 13.7 AuditPackage の証憑添付 (§7 (d) と連携)

§7 (d) ワークフロー方式で監査者にスナップショットを送る際、関連証憑画像も
同梱する場合:

```
クライアント (AuditPackage 作成):
  1. 仕訳セットを auditor の公開鍵で暗号化 (§7 (d))
  2. 関連 voucher 画像も復号 → auditor 公開鍵で再暗号化
  3. AuditPackage に画像 BLOB を含める (or 別の attachment テーブル)
```

実装詳細は E5 (監査連携) で確定。E4 ではフックを残しておく形。

### 13.8 ストレージ抽象化 (Local / S3) との両立

現行 `app/services/storage.py` の `LocalStorage` / `S3Storage` を維持:

- 違いは「バイト列を Local / S3 に書く」のみで、E2EE 化に影響なし
- presigned URL (S3) も暗号文を返すだけなので OK
- ETag / Content-Type は暗号文に対するもの (`application/octet-stream`)

### 13.9 マイグレーション戦略

```
Phase E4 マイグレーション:
  1. vouchers テーブルに新カラム ADD:
     - encrypted_meta_blob, meta_iv (NULL)
     - file_hash_plain (NULL)
     - thumbnail_key (NULL)
  2. メンテナンスウィンドウで全画像を暗号化変換:
     - サーバ側で「Fernet 復号できる API キー」と同じ要領で、平文画像を読み
       一時 MK で暗号化 → ストレージに上書き
     - file_hash (v4.x) → file_hash_cipher にリネーム / コピー
  3. ユーザーが鍵設定完了後、クライアントが再暗号化:
     - 一時 MK で復号 → 自分の MK で再暗号化 → 再 upload
     - file_hash_plain をクライアントが計算して PUT
  4. 全件再暗号化完了後、サーバから一時 MK 破棄
```

注意: 画像はデータ量が大きいので、移行所要時間は仕訳の数十倍。バッチ処理 +
進捗 UI が必須。E7 のメンテナンスウィンドウで E3 と並行して進める。

#### 一時 MK の安全性と運用

§6 と §10.2 / §12.9 で扱った「一時 MK」の E4 への適用詳細:

1. **生成・配布**:
   - サーバ管理者が `flask migration-genkey` CLI で生成 (32B random)
   - サーバ DB に `users.migration_temp_mk` (一時カラム、暗号化なし) で保管
   - 移行期間外は NULL のみ
2. **再暗号化の進捗追跡**:
   - `vouchers.encrypted_meta_blob IS NULL` の件数を `users` ごとに集計
   - 集計値を `users.migration_pending_vouchers` (smallint, デバッグ用) に
     キャッシュ
   - クライアントが PUT で再暗号化するたびに -1 デクリメント
3. **未完ユーザーの扱い**:
   - 移行猶予期間 (§6 の 30 日) 経過後も未完了のユーザー: アカウントロック
     → 「鍵設定完了するか退会するか」を選択させる UI
   - 退会選択時はサーバが一時 MK で復号して CSV エクスポート (E2EE 完了前の
     最後のチャンス、ユーザー同意の上)
4. **一時 MK 廃棄条件**:
   - 全アクティブユーザーの `migration_pending_vouchers = 0` を確認
   - 管理者が `flask migration-finalize` CLI を実行
     - 全 `users.migration_temp_mk` を SQL UPDATE で NULL
     - 関連ストレージ上の旧 Fernet 鍵 (SECRET_KEY 経由) もローテーション
   - 廃棄後は復元不可能 → ロックされたユーザーのデータは復号不能 (規約で明示)
5. **ロスト防止**:
   - 廃棄前のスナップショットをサーバ管理者が S3 など別ストレージに 30 日
     保管 (緊急時の復旧用、運用ガイドに明記)
6. **一時 MK の保管リスクと緩和策**:
   - `migration_temp_mk` は DB 平文保存。移行ウィンドウ中に DB が侵害されると
     **全ユーザーの一時 MK が漏洩し、移行中データが解読可能** になる
   - 緩和策の選択肢 (運用ガイドで規定):
     - **(a) HSM / KMS 連携**: AWS KMS, GCP KMS, Azure Key Vault 等の外部
       鍵管理サービスで一時 MK を保管。サーバアプリは KMS 経由で復号操作
       のみ可能
     - **(b) 短時間保持**: メンテナンスウィンドウ (数時間) のみ DB 平文保管、
       終了後即 NULL クリア。簡易だが運用が固い
   - v5.0 リリース時は (b) を最小要件、(a) を推奨として運用ガイドに記載

注: §12.9 (E3) の一時 MK についても同じリスクと緩和策が適用される (本節を
参照)。

### 13.10 E4 完了条件

- [ ] `vouchers` テーブルに `encrypted_meta_blob`, `meta_iv`, `file_hash_plain`, `thumbnail_key` カラム追加マイグレーション
- [ ] `voucher_audit_logs` に `encrypted_detail_blob`, `detail_iv` 追加
- [ ] 2 段階 upload (init で voucher_id 採番 → AAD に含めて実体 upload) の API 設計確定
- [ ] 画像本体のクライアントサイド AES-GCM 暗号化 (AAD `vimg\0` + user_id + voucher_id)
- [ ] サムネイル生成のクライアントサイド化 (canvas, 200x200, AAD `vthumb\0` + ...)
- [ ] `file_hash_plain` (クライアント計算) と `file_hash_cipher` (サーバ計算) の二重化
- [ ] サーバ側 Pillow サムネイル生成コード (`image.py`) の廃止
- [ ] サーバ側 AI 解析 (`ai_receipt.py`) のクライアントサイド移行 (§11.3, §12.6)
- [ ] presigned URL 取得 → クライアント復号フロー (S3 / Local 両対応)
- [ ] テスト: AAD `vimg` / `vthumb` 攻撃で復号失敗
- [ ] テスト: file_hash_plain / file_hash_cipher の両方が改ざん検出に効く
- [ ] テスト: 電帳法 Phase 2 検索 (日付 / 金額 / 取引先) がクライアントサイドで動作
- [ ] テスト: VoucherAuditLog の detail 暗号化が機能
- [ ] テスト: S3 / Local 両ストレージで E4 が動作
- [ ] AuditPackage への証憑同梱用フック (E5 向け stub / API 設計メモ) の確認
- [ ] 一時 MK 運用 CLI (`flask migration-genkey` / `flask migration-finalize`) の実装と運用ガイド整備

### 13.11 E5 (監査連携) への影響

E4 で確立されるパターン:

- 画像 BLOB の AES-GCM 暗号化 (AAD = テーブル種別 + user_id + entity_id)
- file_hash の plain / cipher 二重化 (改ざん防止 + サーバ独立検証)
- ストレージ抽象化 (Local / S3) と暗号化の両立

E5 では AuditPackage の暗号化 (X25519 公開鍵 + AES-GCM のハイブリッド) と
画像同梱 (E4 で復号 → 監査者公開鍵で再暗号化) を組合せる。

---

## 14. E5 設計スケッチ (監査連携の実装着手レベル詳細)

§7 (d) 非同期ワークフロー方式の実装着手レベル設計。HPKE ハイブリッド暗号、
audit_packages / audit_responses テーブル、TOFU + fingerprint UI、マルチ
ラウンド管理、Voucher 添付。

### 14.1 audit_packages テーブル

```
audit_packages
| カラム             | 型           | 制約                              | 用途 |
|------------------|-------------|-------------------------------------|------|
| id               | bigserial   | PK                                  | |
| audit_grant_id   | bigint      | FK audit_grants.id ON DELETE CASCADE| |
| round_id         | int         | NOT NULL                            | 連番、(audit_grant_id, round_id) で UNIQUE |
| owner_user_id    | bigint      | FK users.id (denormalized)          | サーバ側フィルタ用、テナント分離 |
| auditor_user_id  | bigint      | FK users.id (denormalized)          | サーバ側フィルタ用 |
| permission_level | smallint    | NOT NULL                            | 1 (Lv1) / 2 (Lv2) / 3 (Lv3) |
| ephemeral_pubkey | bytea (32)  | NOT NULL                            | HPKE encapsulated key (送信側 ephemeral X25519 pub) |
| ciphertext       | bytea       | NOT NULL                            | HPKE 暗号文 (仕訳 / 残高 / 画像のスナップショット JSON) |
| snapshot_hash    | bytea (32)  | NOT NULL                            | SHA-256(plaintext snapshot) 改ざん検出 |
| created_at       | timestamptz | NOT NULL                            | |
| expires_at       | timestamptz | NOT NULL                            | created_at + 90 days (FS / TTL) |
| owner_accepted_at | timestamptz | NULL                               | owner が採用を確定した時刻 (NULL = 未対応 or 差戻し)。§14.2 参照 |

UNIQUE (audit_grant_id, round_id)
INDEX (owner_user_id)
INDEX (auditor_user_id)
INDEX (expires_at)  -- 自動削除バッチ用
```

### 14.2 audit_responses テーブル

```
audit_responses
| カラム             | 型           | 制約                              | 用途 |
|------------------|-------------|-------------------------------------|------|
| id               | bigserial   | PK                                  | |
| audit_package_id | bigint      | FK audit_packages.id ON DELETE CASCADE | 対応する依頼 |
| response_type    | text        | CHECK (response_type IN ('revision', 'rejection')) | 修正案 / 差戻し |
| ephemeral_pubkey | bytea (32)  | NOT NULL                            | HPKE (auditor → owner 方向) |
| ciphertext       | bytea       | NOT NULL                            | HPKE 暗号文 (修正案 JSON or 差戻し理由) |
|                  |             |                                     | 修正案 JSON: `{v:1, response_type, summary?, comments?:[{entry_id, ref?, note?, proposal?}]}` |
|                  |             |                                     | `proposal`: `{date, description, lines:[{account_code, debit, credit, description?}]}` = §14.9 構造化置換案 |
| created_at       | timestamptz | NOT NULL                            | |
| expires_at       | timestamptz | NOT NULL                            | 同 90 days |
| owner_acknowledged_at | timestamptz | NULL                          | owner が確認した時刻 |

INDEX (audit_package_id)
INDEX (expires_at)
```

#### 採用 (acceptance) の扱い

owner が「採用」を選んだ場合は **AuditResponse は作成しない**。理由:

- 修正案の採用 = E3 フローで仕訳を更新すること (AuditResponse とは別の操作)
- auditor 側は「対応する仕訳が更新された」ことを次回スナップショットで観察
  可能 (差分表示)
- 別 endpoint で `audit_packages.owner_accepted_at` を更新し、auditor 画面に
  「採用済」表示
  ```
  POST /api/v1/audit-packages/<id>/accept  → owner_accepted_at = NOW()
  ```
- `audit_packages` に `owner_accepted_at timestamptz NULL` カラムを追加

**個別 proposal の 1 クリック採用 (§14.9) と package accept は別操作**:

- `comments[].proposal` (構造化置換案) を持つ修正案は、owner が「採用」ボタンで
  **当該仕訳 1 件を置換**する (後述 §14.9。`PUT /api/v1/journals/<id>` で全置換)。
- `owner_accepted_at` (上記 `/accept`) は **その監査ラウンド全体を「対応完了」と締める**
  操作で、個別仕訳の置換とは独立。両ボタンは UI 上で共存する。

これにより auditor は (a) 採用 / (b) 差戻し / (c) 修正案 (仕訳置換) の
3 つの結果を区別できる。

#### HPKE base mode の送信者認証

HPKE base mode では **受信者は送信者の真正性を暗号的に検証できない** (送信者
署名なし)。本設計では以下で補完:

- **サーバ認証 (Bearer / Session)**: POST `/api/v1/audit-packages` 時に
  サーバが `current_user.id == owner_user_id` を検証
- **TOFU pinning (§14.4)**: owner クライアントは auditor の公開鍵を
  IndexedDB に固定 → 中間者攻撃 (サーバが公開鍵すり替え) を検知
- 将来の auth mode 移行検討時には HPKE auth mode (送信者認証付き) を採用
  する選択肢あり (`@hpke-js/core` の Auth Mode サポート要)

### 14.2.1 Lv1 / Lv3 のスナップショット内容

§14.5 は Lv2 を例にしたが、Lv1 / Lv3 のスナップショット内容も明示:

```
Lv1 (集計のみ):
  snapshot = {
    "v": 1,
    "level": 1,
    "trial_balance": {...},     # 試算表 (年度集計)
    "profit_loss": {...},       # 損益計算書
    "balance_sheet": {...}      # 貸借対照表
  }
  仕訳本体は含まない、auditor は数字のみ見る

Lv2 (税務科目限定):
  snapshot = {
    "v": 1,
    "level": 2,
    "entries": [<税務科目に該当する仕訳のみ>],
    "vouchers": [<該当仕訳の証憑画像>]
  }
  owner クライアントがフィルタした分だけ

Lv3 (全権限相当):
  snapshot = {
    "v": 1,
    "level": 3,
    "entries": [<全仕訳>],
    "vouchers": [<全証憑画像>],
    "medical_expenses": [<医療費>],
    "balance_caches": [<確定済残高>]
  }
  全データを送信、Lv2 のフィルタなし
```

Lv3 は元の代理閲覧 UX (リアルタイム閲覧) と異なり、**「スナップショット時点
の全データを auditor に渡す」セマンティクスに変わる**。auditor は閲覧後に
新仕訳を作成できないので、修正案を返して owner が反映するワークフローのみ。

これは v4.x の Lv3 ユーザーには大きな UX 変更だが、E2EE 純粋性を保つための
必然的トレードオフ (§7 (d) 採用理由 #1)。

### 14.3 HPKE (RFC 9180) フロー

ephemeral 鍵 + AEAD でフォワードセクレシーを実現。
**以下の擬似コードは HPKE ライブラリ内部処理の解説**。実装側は HPKE ライブラリ
(`@hpke-js/core` / `hpke-py`) の高レベル API
(`createSenderContext` / `seal` / `open`) を呼び出すだけで内部的に同じことが
行われる。:

```
[owner クライアント] 送信時:
  1. snapshot = MK で復号した仕訳 / 残高 / 画像のフィルタ結果 (JSON)
  2. snapshot_hash = SHA-256(snapshot)
  3. ephemeral_priv, ephemeral_pub = X25519 ペア生成
  4. shared_secret = X25519(ephemeral_priv, auditor.public_key)
  5. key, base_nonce = HKDF(shared_secret, info="iikanji-audit-package-v1", L=44)
     ※ HPKE base mode の標準フロー
  6. aad = b"ap" (2B) + uint64_be(audit_package_id) (8B) + uint32_be(round_id) (4B) = 14B
     ※ §12.2 と同様、固定長エンコードのためセパレータ \0 はオプション
     (本仕様では含めない。実装時は HPKE ライブラリの aad パラメータに 14B を渡す)
  7. ciphertext = AES-256-GCM.Seal(key, base_nonce, snapshot, aad)
  8. ephemeral_priv を破棄 (フォワードセクレシー)
  9. POST /api/v1/audit-packages { ephemeral_pubkey, ciphertext, snapshot_hash, ... }

[auditor クライアント] 受信時:
  10. GET /api/v1/audit-packages/<id> → ephemeral_pubkey, ciphertext, snapshot_hash
  11. shared_secret = X25519(auditor.private_key, ephemeral_pubkey)
  12. key, base_nonce = HKDF(shared_secret, info="iikanji-audit-package-v1", L=44)
  13. aad = b"ap" + uint64_be(audit_package_id) + uint32_be(round_id) (14B、送信側と同一)
  14. snapshot = AES-256-GCM.Open(key, base_nonce, ciphertext, aad)
  15. SHA-256(snapshot) == snapshot_hash で改ざん検証
```

返信 (audit_response) も同じパターンで owner の public_key を使用。

#### フォワードセクレシーの効果

- ephemeral_priv はサーバに保存されない (作成直後に破棄)
- auditor の long-term private_key が将来漏洩しても、過去の audit_packages
  は復号できない (ephemeral_priv が必要だが破棄済)
- 90 日 TTL 後はサーバから自動削除されるので、長期的なリスクも限定的

### 14.4 X25519 公開鍵の真正性検証 (TOFU + fingerprint)

§7 で確定済の TOFU フローを実装:

```
owner クライアント:
  - 初回 AuditGrant 作成時にサーバから auditor.public_key を取得
  - SHA-256(public_key) の最初の 20 バイトを Base32 エンコード →
    "iikanji-AUDITOR-XXXX-XXXX-XXXX-XXXX" 形式の fingerprint を表示
  - owner UI で「この fingerprint を auditor 本人に電話 / 対面で確認」
    と促す
  - 確認チェックボックスを押すと auditor.public_key を IndexedDB に pinning
  - 以降、サーバが返す public_key が変わったら警告ダイアログ
```

データモデル:

```
client-side (IndexedDB):
  pinned_auditor_keys = {
    auditor_user_id: { public_key_sha256, pinned_at }
  }
```

サーバ側に「fingerprint 確認済」フラグを置くと意味がないので、すべて
クライアント側で管理。

#### IndexedDB クリア時の UX

ユーザーが「ブラウザデータ削除」「履歴クリア」等で IndexedDB を消した場合、
TOFU の pinning が失われる。挙動:

- pinning が消えた場合: **再 fingerprint 確認を促す** ダイアログを表示
  (サイレント再ピンニングは中間者攻撃を見逃すリスクがあるため不採用)
- ダイアログ文言: 「セキュリティ情報がリセットされました。監査者本人に
  fingerprint を再確認してください」
- ユーザーが確認 → 再 pinning。新しい fingerprint が旧と一致するなら問題なし、
  異なる場合は警告 (公開鍵がすり替わった可能性)
- このフローは v4.x の Passkey 再登録と類似の UX

### 14.5 監査フロー全体 (Lv2 を例に)

```
[owner クライアント]
1. /settings/audit-grants で auditor を選択
2. AuditGrant.permission_level = 2 を設定、税務科目を選択 (account_user_id リスト)
3. クライアントが暗号化済仕訳から税務科目該当行のみ抽出 → 復号 → snapshot 作成
4. auditor.public_key を取得 → fingerprint 確認 → 暗号化
5. POST /api/v1/audit-packages → round_id = 1

[auditor クライアント]
6. 監査ダッシュボード → AuditPackage round_id=1 取得
7. 自分の MK で X25519 秘密鍵をアンラップ → HPKE 復号
8. snapshot を仮想的な「監査用試算表」として表示
9. 修正案を作成 (JSON: [{entry_id, changes: {...}}, ...])
10. owner.public_key で暗号化 → POST /api/v1/audit-responses

[owner クライアント]
11. AuditResponse 取得 → 復号 → 修正案を画面表示
12. 「採用」「差戻し」を選択
   - 採用: クライアントが新仕訳を作成して MK 暗号化 → POST (E3 フロー)
   - 差戻し: 何もしないか、新 round_id=2 で再スナップショット送信

オプション (再ラウンド):
13. owner が新たな snapshot を作成して POST → round_id=2
14. auditor は最新 round のみ作業可能 (UI で旧 round はロック表示)
```

### 14.6 Voucher 同梱 (§13.7 連携)

監査対象に証憑画像を含める場合:

```
owner クライアント:
  1. snapshot に "vouchers" 配列を追加:
     [{voucher_id, image_bytes_base64, mime, original_filename}, ...]
  2. image_bytes は MK 復号後の平文を base64 エンコード
  3. 全体を HPKE で auditor.public_key 暗号化
  ※ 1 AuditPackage に複数画像、合計サイズは API 上限 (例 10MB) 内
  ※ 大きい画像は別の AuditPackageAttachment テーブルで分割 (E5 で検討)
```

代替案: `audit_package_attachments` テーブルを新設して画像を別 BLOB 化。
スナップショット JSON は軽量に保ち、画像は個別取得。E5 実装時に確定。

### 14.7 マルチラウンドレビュー

`(audit_grant_id, round_id)` UNIQUE で連番管理:

- owner は新 round 作成前に未処理 AuditResponse がないか確認
- 未処理がある場合は警告: 「監査者から未処理の修正案があります。先に対応
  してください」
- 強制で新 round 作成も可能 (差戻し扱い + 警告ログ)
- auditor は最新 round のみ作業可能、旧 round は read-only (UI)
- AuditPackage 削除は CASCADE で AuditResponse も削除 (90日 TTL での自動削除)

### 14.8 自動削除と TTL

`expires_at = created_at + 90 days` を CLI で実行:

```
flask audit-cleanup
  - DELETE audit_packages WHERE expires_at < NOW()
  - CASCADE で audit_responses も削除
```

§10.5 の `flask rotate-cleanup` と同じく cron / systemd timer で 1 時間ごと
起動を想定。

### 14.9 監査者 UX (差分表示・コンフリクト解決)

修正案の差分 UI:

```
auditor 画面:
  | 日付       | 借方科目 | 借方金額 | 貸方科目 | 貸方金額 | 摘要       |
  | 2026-05-22 | 通信費   | 5,000   | 現金     | 5,000   | 携帯料金   | ← 現状
  | 2026-05-22 | 通信費   | 5,000   | 普通預金 | 5,000   | 携帯料金   | ← 修正案 (赤強調)
                                      ^^^^^^^^^ 差分表示
```

owner 画面:

```
| 仕訳 | 監査者の修正案                | 採用 | 差戻し |
| #123 | 貸方科目 現金 → 普通預金     | ☐    | ☐     |
| #124 | 摘要 「電話代」→「通信費」  | ☐    | ☐     |
```

採用すると owner クライアントが当該 `JournalEntry` を **`PUT /api/v1/journals/<id>`
で全置換**する (旧明細を DELETE + 新明細を INSERT する 1 トランザクション)。
当初案の「旧仕訳の削除 + 新仕訳の作成」より優れる:

- **entry_id を保つ**ので `Voucher.journal_entry_id` (ON DELETE SET NULL) が孤立せず、
  証憑リンクが維持される。
- 1 commit で atomic。AAD は Option B (`buildAAD("je"/"jel", user_id)`、entry_id 非依存)
  なので同一 entry_id への再暗号化で AAD 不一致は起きない。

owner クライアントは proposal の `{date, description, lines}` を自分の MK で再暗号化
(`buildJournalEntry`)、source / fiscal_period は現行仕訳から引き継ぐ。確定済み期間・
科目存在・貸借一致は **サーバ側 (`check_entry_modifiable` / `check_period_open_for_new`
/ PUT 検証) が権威**で、フロントのガードは UX 目的。差戻しは何もしない (新ラウンド送信で対応)。

実装ノート (実装時の整合): owner レビュー画面は復号した現行仕訳 (`fetchEntryForDiff`)
と proposal を `computeEntryDiff` で**位置 (index) 対応**で突合して差分表示する。採用 PUT も
proposal の行順をそのまま送るため、差分表示と置換結果の行対応が一致する。

### 14.10 権限取消の単純化

§7 (d) で確定済の通り、(a/b/c) 共通の「MK ローテーション」は **不要**:

- AuditGrant.revoked_at をセット
- 既存 AuditPackage は 90 日 TTL で自動消滅
- 監査者は AuditGrant.status='revoked' なら新規 GET 拒否

E5 実装で `AuditGrant.revoked_at` を見てサーバ側で 403 を返すロジックを追加。
クライアント側の鍵ローテーションは不要。

### 14.11 E5 完了条件

- [ ] `audit_packages` / `audit_responses` テーブル新設マイグレーション
- [ ] `users.public_key` カラム追加 + 登録時の自動 X25519 鍵ペア生成 (E1 と連動)
- [ ] HPKE (RFC 9180 base mode) のクライアント実装 (Web: `@hpke-js/core` 等 / Python: `hpke-py`)
- [ ] `/api/v1/audit-packages` GET / POST + サーバ側 owner_user_id / auditor_user_id フィルタ (IDOR 防止)
- [ ] `/api/v1/audit-responses` GET / POST
- [ ] TOFU + fingerprint 確認 UI (owner 設定画面、IndexedDB pinning)
- [ ] 監査者ダッシュボードの差分表示 UI
- [ ] owner 側の修正案レビュー UI (採用 / 差戻し)
- [ ] マルチラウンド管理 (未処理 response 警告、最新 round のみ作業可)
- [ ] AuditGrant.revoked_at による新規 GET 拒否
- [ ] `flask audit-cleanup` CLI (90 日 TTL の自動削除、cron / systemd timer 連動)
- [ ] v4.x の Lv2 リアルタイム閲覧 UX (auditor がログインして閲覧) の廃止
- [ ] **段階的告知計画**:
  - v4.x 最終マイナー (例 v4.9.0) で auditor ダッシュボードに deprecation
    バナー表示「v5.0 から監査連携は非同期ワークフロー方式に変わります」
  - v5.0 リリース 1 ヶ月前にメール通知 (§6 と統合)
  - v5.0 移行猶予期間中は dual UX (v4.x スタイルと v5.0 ワークフロー両対応)
    は実装しない (UX が複雑化するため)
  - 猶予期間後はワークフロー方式のみ、リアルタイム閲覧 UI は完全削除
- [ ] 既存 AuditGrant ユーザーへの移行ガイド (v5.0 で非同期ワークフローに切替)
- [ ] `audit_packages.owner_accepted_at` カラム追加 + `/accept` endpoint
- [ ] テスト: HPKE 復号失敗 (rogue ephemeral_pubkey すり替え) → 検知
- [ ] テスト: AAD すり替え (他 round_id への入れ替え) → 復号失敗
- [ ] テスト: owner_user_id / auditor_user_id フィルタ IDOR (他者の package が取れない)
- [ ] テスト: revoked AuditGrant への POST 拒否
- [ ] テスト: 90 日 TTL 経過の自動削除

### 14.12 E6 (クライアント全面対応) への影響

E5 で確立されるパターン:

- HPKE ハイブリッド暗号 (ephemeral + AEAD) によるフォワードセクレシー
- TOFU + fingerprint による信頼境界の構築
- スナップショットベースの非同期ワークフロー
- 90 日 TTL による自動廃棄

E6 では:
- 全クライアント (Web / client-py / client-tui) で同じ HPKE 仕様を実装
- `iikanji-mcp` の配布停止
- 全画面の E2EE 対応完了
- データ一括 zip ダウンロード (確定事項表)

---

## 15. E6 設計スケッチ (クライアント全面対応 + データ一括 zip ダウンロード)

E1–E5 で確立した暗号化基盤を **全クライアント (Web / client-py / client-tui)** に
適用し、E2EE 化を完成させる。MCP サーバの配布停止、Webhook / 自家ホスト LLM の
廃止、データ一括 zip ダウンロードもこのフェーズで実装。

### 15.1 全クライアントの対応マトリクス

> **更新 (E6 トラック3):** `client-tui` は E2EE 移植を行わず **v5.0 で廃止・
> リポジトリ archive 化**する (`iikanji-mcp` と同様)。CLI クライアントの E2EE
> 対応は `client-py` に一本化する。以下マトリクスの client-tui 列は v4.x まで
> の到達点であり、v5.0 では「廃止」。

| 機能 | Web | client-py | ~~client-tui~~ (廃止) | iikanji-mcp |
|---|---|---|---|---|
| Master Key 管理 (§10) | ✅ SharedWorker (リロード跨ぎ・全タブ共有・60 分 idle 自動ロック) | ✅ OS keyring | ✅ OS keyring | ❌ E2EE 非両立 |
| Passkey PRF | ✅ WebAuthn | — (Bearer + パスフレーズ) | — | — |
| API キー設定 (§11) | ✅ クライアント暗号化 | ✅ ローカル復号して直接 LLM | ✅ 同左 | ❌ 廃止 |
| 仕訳 CRUD (§12) | ✅ JSON-then-encrypt | ✅ 同左 | ✅ 同左 | ❌ 廃止 |
| CSV/OFX/Web import | ✅ JS パース | ✅ Python パース | ✅ 同左 | — |
| AI 証憑解析 (§13.6) | ✅ ブラウザ fetch 直接 | ✅ 直接 LLM 呼出 | ✅ 同左 | ❌ 廃止 |
| 証憑画像 (§13) | ✅ canvas + AES-GCM | ✅ Pillow + AES-GCM | ✅ 同左 | ❌ 廃止 |
| サムネイル生成 | ✅ クライアント | ✅ クライアント | ✅ 同左 | — |
| 監査ワークフロー (§14) | ✅ HPKE | ✅ hpke-py | ✅ 同左 | ❌ 廃止 |
| レポート集計 (§12.3) | ✅ クライアント | ✅ pandas etc | ✅ 同左 | — |
| 検索 (§12.4) | ✅ JS フィルタ | ✅ Python filter | ✅ 同左 | — |
| データ一括 zip DL (§15.4) | ✅ Web Worker | ✅ CLI コマンド | — | — |

### 15.2 MCP サーバ (`iikanji-mcp`) の配布停止

確定事項表通り **v5.0 で配布停止**:

- 理由: MCP は Claude Desktop 等の LLM に平文データを渡すため E2EE と本質的に
  両立不可
- リポジトリ: `nananek/iikanji-kakeibo-client-mcp` を archive 化
- README に「v4.x 以前で利用可、v5.0 では使えません」と明記
- v4.x 最終版で deprecation 警告を表示
- 代替案を README に追記:
  - Claude Desktop で家計簿データを使いたい場合は、`client-py` でローカル
    エクスポート → 手動でファイルアップロードするしかない
  - これは E2EE と AI の根本的なトレードオフであり、本サービスでは E2EE を
    優先する

### 15.3 Webhook と自家ホスト LLM の廃止確認

§4 / §11.3 / §12.6 で言及済みの廃止を E6 でハードコードレベルで確定:

```
v5.0 マイグレーション時に DELETE する v4.x データ:
- webhook_configs (全行)
- ai_drafts.discord_webhook_url (カラム DROP)
- user_ai_configs WHERE provider = 'llama_cpp' (廃止 provider)
  → ユーザーには移行ガイドで「自家ホスト LLM は廃止、OpenAI/Anthropic などの
    BYOK に切替えてください」と案内
```

`flask v5-migrate-cleanup` CLI で実行:

```
flask v5-migrate-cleanup --dry-run  # 削除対象を表示
flask v5-migrate-cleanup --execute  # 実行
```

### 15.4 データ一括 zip ダウンロード

確定事項表で「クライアントサイド復号 + バックグラウンド zip 生成 + メール通知」
と決定済み。実装詳細:

#### フロー (Web)

```
1. ユーザーが /settings/export をクリック
2. クライアントが暗号化された全データを取得 (journal_entries, vouchers,
   medical_expenses, balance_cache_blobs)
3. Web Worker 内で:
   a. 各テーブルを MK で復号 (E3 / E4 のフロー)
   b. CSV 形式に変換 (仕訳帳 / 元帳 / 証憑メタデータ等)
   c. 証憑画像も平文に戻して画像ファイルとして含める
   d. JSZip 等で zip 化 (バンドルサイズ: ~50 KB minified)
4. zip を直接ブラウザでダウンロード (Blob URL) OR 大きすぎる場合はサーバに
   一時アップロード:
   - 暗号化 zip (= AES-GCM(MK, ...) で再暗号化) をサーバに POST
   - サーバが presigned URL を 24 時間有効で発行
   - ユーザーのメールに DL リンクを送信 (送信失敗時はサイト内通知)
   - 期限切れで自動削除
```

#### フロー (client-py)

```
iikanji export --output-dir ./backup --format zip
  - ローカル MK で全データを復号
  - ./backup/ に CSV + 画像をディレクトリ構造で展開、または zip で一括出力
  - サーバ往復不要、完全ローカル処理
```

#### 大きいデータの扱い

仕訳 10 万件 + 証憑 1 GB のユーザーを想定:

- Web Worker での復号 5 秒以内 (§12.3 パフォーマンス目標)
- 証憑画像復号は 1 枚 ~10ms × 千枚 = 10 秒程度
- zip 化は **`fflate` で chunk-by-chunk stream 処理**:
  - `pako` は deflate のみで zip 形式を生成できないので不採用
  - `fflate` (MIT, ~10KB minified) は zip stream API あり、Web Worker 対応
  - 証憑画像を 1 枚ずつ復号 → zip stream に追記 → 解放 (メモリピーク抑制)
- メモリピーク試算: 暗号文 + 平文 + zip バッファで暗号文サイズの 3 倍が上限。
  証憑 200 MB 超のユーザーは Chrome のメモリ制限 (2GB) に近づくため
  **client-py 推奨閾値**:
  - 証憑 200 枚超 OR 合計 200 MB 超の場合は Web UI で警告表示 + client-py 案内
- 大規模ユーザーは client-py で完全ローカル処理 (バッチサイズ無制限)

#### メール通知

```
件名: 「いいかんじ™家計簿: データエクスポートが完了しました」
本文:
  YYYY/MM/DD HH:MM に開始されたエクスポートが完了しました。
  以下の URL から 24 時間以内にダウンロードしてください:

  https://example.com/exports/abc123

  ※ このリンクは E2EE 暗号化されたファイルへのリンクです。
    ダウンロード後、サイトで自分の MK を使って復号してください。
```

#### サーバ側 export_jobs テーブル

```
export_jobs
| カラム            | 型           | 用途 |
|-----------------|--------------|------|
| id              | bigserial    | PK |
| user_id         | bigint       | FK |
| status          | text         | pending / generating / ready / expired / failed |
| storage_key     | text         | ストレージパス (Local/S3) |
| created_at      | timestamptz  | |
| ready_at        | timestamptz  | NULL |
| expires_at      | timestamptz  | created_at + 24 hours |
| download_count  | smallint     | 0 (`EXPORT_MAX_DOWNLOADS` まで、デフォルト 3) |

INDEX (user_id, expires_at)
```

### 15.5 退会フローとの統合 (Phase 4 #94)

§15.4 のデータ一括 zip は退会フローの前段として位置付け:

```
退会フロー (v5.0):
  1. /settings/account/delete を選択
  2. 「退会前にデータをダウンロードしますか?」UI
  3. はい → §15.4 のフロー → ダウンロード完了確認後、退会へ
  4. いいえ → 即時退会 (データ完全削除、復元不能)
```

E2EE 下では退会後の救済 (運営者によるリカバリ) は不可能なので、ダウンロード
の重要性をユーザーに強調表示。

### 15.6 既存機能の E2EE 適合性チェックリスト

v4.x の全機能を E6 で監査:

- [x] 出納帳 (cashbook) — E3 のフローで対応
- [x] 仕訳帳 (journal) — E3
- [x] 医療費管理 (medical) — E3 (medical_expenses)
- [x] レポート全般 — E3 クライアントサイド集計
- [x] 勘定科目管理 (accounts) — 平文維持で OK (`account_user_id` FK のみサーバが参照)
- [x] CSV / OFX / Web import — §12.5
- [x] AI 証憑仕訳 — §11.3 / §13.6
- [x] 設定全般 (UserAIConfig 等) — §11
- [x] 月次確定 (FiscalClose) — §12.7
- [x] Passkey — §10
- [x] 監査連携 (AuditGrant) — §14
- [x] 証憑 — §13
- [x] REST API (api blueprint) — Bearer + MK の二段階 (§10.6)
- [x] OAuth (Device Flow) — Bearer 単独で動作 (MK は client-py 側保持)
- [ ] **Auto Import** (auto_import_sources) — 廃止 (サーバ側で WebDAV
      フェッチして AI 解析する設計、E2EE と非両立)
- [ ] **Webhook (WebhookConfig)** — 廃止
- [ ] **iikanji-mcp** — 廃止

Auto Import (WebDAV) は v4.x で既にオプトアウト済 (CLAUDE.md記載) なので、
v5.0 で完全削除。

### 15.7 E6 完了条件

- [ ] Web クライアントの全機能 E2EE 化 (E3 で開始、E4 / E5 で連動、E6 で完了)
- [ ] `client-py` の全 API endpoint E2EE 対応 (Bearer + ローカル MK)
- [ ] `client-tui` の全画面 E2EE 対応
- [ ] `iikanji-mcp` リポジトリの archive 化と README 更新
- [ ] webhook_configs / discord_webhook_url / llama_cpp provider の DROP マイグレーション
- [ ] auto_import_sources / auto_import_processed_files の DROP
- [ ] `/settings/export` UI (Web): Web Worker での zip 生成 + メール通知
- [ ] `iikanji export` CLI (client-py)
- [ ] `export_jobs` テーブルマイグレーション
- [ ] `flask export-cleanup` CLI (期限切れ自動削除、auto_abort 系と統一)
- [ ] 退会フローと export の統合 UI
- [ ] v4.x → v5.0 移行ガイド (README + リリースノート)
- [ ] テスト: Web Worker zip 生成のメモリプロファイル (大規模ユーザー想定)
- [ ] テスト: メール通知の DL リンクが有効期限切れで 410 Gone を返す
- [ ] テスト: 退会後のデータが復元不能 (DB 全 NULL クリア、ストレージ削除)

### 15.8 E7 (一斉移行) への影響

E6 までで実装が完成し、E7 はそれを稼働中サービスに適用する **運用フェーズ**:

- 全 v4.x ユーザーへの事前通知
- メンテナンスウィンドウの実施
- §6 のフローに従ったサーバ生成鍵 → ユーザー鍵への移行
- 移行完了後のサーバ生成鍵の完全削除
- E7 完了時点で **v4.x のサーバサイド AI / Fernet 等は完全廃止**

---

## 16. E7 設計スケッチ (一斉移行 + メンテナンスウィンドウ実行)

E1-E6 で実装が完了した状態で、稼働中サービスを v5.0 (E2EE) に切り替える
**運用フェーズ**。§6 の一斉移行戦略を具体化する。

### 16.1 タイムライン

```
v5.0-beta リリース 1 ヶ月前:
  - 既存ユーザー全員にメール通知 (§6 スケジュール参照)
  - サインイン時に「v5.0 移行のお知らせ」ダイアログ
  - v4.x のステータスバナーに deprecation 警告

メンテナンスウィンドウ前日:
  - 最後のリマインダーメール
  - ステータスページ更新

メンテナンスウィンドウ当日 (例: 土曜 0:00-6:00 JST):
  - 0:00 サービス停止 (read-only モード or 完全停止)
  - 0:30 DB バックアップ完了 (フルダンプ + S3 スナップショット)
  - 1:00 マイグレーション実行開始
    - E1-E6 の全マイグレーション (046_wrapped_keys から開始、§16.3 参照)
    - 一時 MK 生成 (`flask migration-genkey`)
    - 全ユーザーデータをサーバ側で一時 MK 暗号化
  - 4:00 移行完了予定
  - 4:30 動作確認テスト (smoke test)
  - 5:00 サービス再開 (v5.0)
  - 5:30 移行進捗ダッシュボード公開

メンテナンスウィンドウ後 1 ヶ月 (鍵設定猶予期間):
  - 全ユーザーにログイン時の鍵設定ウィザード強制
  - 30 日経過後 + 鍵未設定ユーザーはアカウントロック
  - ロック解除には鍵設定 or 退会選択

鍵設定猶予期間後 3 ヶ月 (一時 MK 廃棄前):
  - 「全ユーザーの再暗号化完了」フラグを確認
  - `flask migration-finalize` で一時 MK を全 NULL クリア
  - サーバ側の Fernet / SECRET_KEY も rotate
  - v4.x コードの完全削除 (server側 AI / Webhook / MCP 等)

完全移行完了 (リリース 4 ヶ月後):
  - v5.0 が正式版に
  - 過去のメンテナンスウィンドウ手順を運用ガイドに残す
```

### 16.2 事前通知の内容

メール通知例:

```
件名: 【重要】いいかんじ™家計簿 v5.0 への移行 (E2EE 化) のお知らせ

本文:
  いつもご利用ありがとうございます。
  X月X日 (土) 0:00-6:00 (JST) にメンテナンスを実施し、v5.0 (E2EE 化) に
  移行します。

  v5.0 では以下が変わります:
  - 全データがクライアント側で暗号化されます (サーバ管理者も見られなくなる)
  - 初回ログイン時にパスフレーズ + リカバリシード (24 単語) の設定が必要
  - メンテ後 30 日以内に鍵設定しないとアカウントがロックされます

  廃止される機能:
  - Discord Webhook 通知
  - 自家ホスト LLM (llama_cpp) 接続
  - MCP サーバ (Claude Desktop 連携)
  - 監査者 Lv2/Lv3 のリアルタイム閲覧 → 非同期ワークフローに変更
  - 自動取込 (WebDAV, 既にオプトアウト済)

  移行に関する FAQ: <URL>
  心配な方は事前にデータエクスポートを推奨: /settings/export
```

### 16.3 マイグレーション順序とロールバック計画

```
マイグレーション順序 (Alembic revision):
  046_wrapped_keys                  # E1 鍵管理基盤
  047_user_ai_configs_blob          # E2 API キー暗号化
  048_journal_entries_blob          # E3 仕訳
  049_journal_entry_lines_blob      # E3 仕訳明細
  050_medical_expenses_blob         # E3 医療費
  051_balance_cache_blobs           # E3 残高キャッシュ
  052_vouchers_blob                 # E4 証憑画像
  053_voucher_audit_logs_blob       # E4 証憑ログ
  054_audit_packages                # E5 監査連携
  055_audit_responses               # E5 監査レスポンス
  056_users_public_key              # E5 X25519 ペア
  057_export_jobs                   # E6 一括 zip
  058_drop_legacy                   # 旧テーブル/カラム DROP (E6)
                                    # webhook_configs / auto_import_sources /
                                    # ai_drafts.discord_webhook_url / etc.
```

各マイグレーションは `down_revision` で連鎖し、`flask db downgrade <prev>` で
1 ステップずつロールバック可能 (Alembic 標準)。

**ロールバック判断基準**:

- マイグレーション中の障害 → **DB バックアップからの完全復元が本命**、
  `flask db downgrade` は補助的 (スキーマだけ戻してもデータが暗号化済の場合は
  復号できないため)。メンテナンスウィンドウ延長
- マイグレーション後の smoke test 失敗 → 同上
- ユーザー鍵設定後の再暗号化エラー → 個別ユーザー対応 (アカウントロック解除
  + サポート対応)

#### DB 復元前のスキーマ事前要件

E1 (046) 着手前に以下の前提マイグレーションが必要 (実装フェーズで確認):

- `User.is_active` カラム (Flask-Login の UserMixin は default True だが、
  §16.5 の鍵未設定ロックでは DB レベルのカラムが必要 → `045b_user_active` 等
  で先に追加)
- `User.migration_temp_mk` カラム (§13.9 の一時 MK 保管用、E1 マイグレーション
  内で `wrapped_keys` と同時追加でも可)

これらの事前準備マイグレーションがないと §16.5 のロックフローが機能しない。

#### 管理者認証 (/admin/migration-progress 等) の前提

§16.6 の管理者ダッシュボードは現行の `user_type` (personal / auditor) では
対応できない。実装時の選択肢:

- (a) 新規 `user_type='admin'` を追加 (`flask db migrate` 後 SUPERUSER 環境変数で
  指定したユーザーを admin に昇格)
- (b) `flask` CLI のみで提供 (Web UI なし)
- (c) 環境変数 `OPS_BASIC_AUTH_USER` / `_PASS` で Basic 認証
  (Flask-HTTPAuth 等)

→ **暫定: (c) を採用**。最も軽量で、運用者のみがアクセスする想定に合致。
E7 実装時に確定。

### 16.4 一時 MK の生成と廃棄手順

§13.9 で詳述した一時 MK 運用の運用ガイド版:

```
[メンテナンスウィンドウ中]
1. サーバ管理者が flask migration-genkey 実行
   - users 全行に migration_temp_mk (32B random) を生成して INSERT
   - 32B は MK と同じ強度
2. 全テーブルを一時 MK で暗号化 (バッチ処理)
3. 暗号化完了後、サービス再開

[鍵設定猶予期間中]
4. 各ユーザーがログイン → 鍵設定ウィザード
5. ウィザード完了時、クライアントが旧 (一時 MK 復号) → 新 (自分の MK 暗号化)
6. サーバが migration_pending_vouchers / migration_pending_entries を
   -1 デクリメント

[一時 MK 廃棄]
7. 全アクティブユーザーの migration_pending = 0 を確認
8. flask migration-finalize 実行
   - 全 users.migration_temp_mk = NULL (SQL UPDATE)
   - 関連 SECRET_KEY (Fernet 用) を rotate
   - 旧バックアップから一時 MK を抽出するリスクを下げるため、ストレージ
     上のバックアップも整理
```

### 16.5 鍵未設定ユーザーの扱い

メンテ後 30 日経過しても鍵未設定の場合:

```
1. アカウントロック (`User.is_active = False` + 専用フラグ)
2. ログイン試行時に「鍵設定するか退会するか」専用 UI を表示
3. 鍵設定: §10 のウィザードフロー
4. 退会: §15.5 のフロー (一時 MK が残っていればエクスポート可能、廃棄後は不可)
5. ロック後 60 日経過 + 退会も鍵設定もしない → 自動退会 (規約で明示、データ
   完全削除)
```

### 16.6 移行進捗ダッシュボード (内部運用)

サーバ管理者向け:

```
GET /admin/migration-progress (要 admin 認証)

レスポンス例:
{
  "total_users": 5432,
  "users_with_keys": 4821,
  "users_locked": 234,
  "users_pending": 377,
  "data_re_encrypted_pct": 89.3,
  "temp_mk_active": true,
  "temp_mk_finalize_eligible": false
}
```

これを Web UI でグラフ表示 (運用ガイドの一環)。

### 16.7 公開後の v4.x コード削除

E7 完了 = 一時 MK 廃棄完了の時点で:

```
削除対象コード:
- app/services/ai_receipt.py の Fernet 暗号化部分 / サーバサイド AI 呼出
- app/services/notify.py (Webhook)
- app/services/auto_import.py
- app/services/balance_cache.py のサーバ集計部分
- app/services/tax.py のサーバ集計部分 (クライアント版が引き継ぐ)
- app/views/api.py の旧 v1 仕訳 endpoint (新 v1 は暗号文専用)
- migrations/ の `Fernet` / `WebhookConfig` 関連

削除方法:
1. 削除専用 PR (`chore(v5): v4.x legacy 削除`) を 1 本作る
2. 全 git history を残しつつ HEAD では完全削除
3. リリースノートで明記
```

### 16.8 障害時の緊急対応

メンテナンスウィンドウ中の障害シナリオ:

| 障害 | 対応 |
|---|---|
| マイグレーション中の DB エラー | 即時ロールバック、バックアップから復元、メンテ延長 |
| マイグレーション完了後、smoke test 失敗 | 同上 |
| 一時 MK 生成失敗 | flask migration-genkey 再実行 (既存行は SKIP) |
| 鍵設定ウィザードのバグ | hotfix リリース、ユーザーに再ログイン依頼 |
| 監査者鍵 (X25519) 生成失敗 | 当該ユーザーに手動対応 |

長期的障害 (鍵設定猶予期間中):
- 大規模なバグ発見 → v4.x ロールバック (一時 MK で復号して旧形式に戻す)
- ただしロールバック後の整合性確認は困難 → 慎重に判断

### 16.9 E7 完了条件

- [ ] メンテナンスウィンドウのスケジュール確定 + 事前通知 (1 ヶ月前 + 前日)
- [ ] DB バックアップ + ストレージスナップショット (S3 / Local 両方)
- [ ] 全マイグレーション (046-058) の実行 + smoke test
- [ ] `flask migration-genkey` で一時 MK 生成 + サーバ側全データ暗号化
- [ ] v5.0 サービス再開 + ステータスページ更新
- [ ] 鍵設定ウィザードの全ユーザーへの強制表示
- [ ] 30 日経過後の未設定ユーザーロック処理
- [ ] `flask migration-finalize` で一時 MK 廃棄 + SECRET_KEY ローテーション
- [ ] v4.x コードの完全削除 PR
- [ ] 移行進捗ダッシュボード (/admin/migration-progress)
- [ ] 運用ガイド更新 (メンテナンスウィンドウ手順、緊急対応手順)
- [ ] 移行後 1 ヶ月のサポート体制強化 (お問い合わせフォーム監視)
- [ ] テスト: バックアップから完全復元できることを事前検証
- [ ] テスト: 鍵設定ウィザード完了後の全データ復号 (smoke test)
- [ ] テスト: 鍵未設定ユーザーのロック挙動

### 16.10 ポスト E7 のフェーズ (E8+: 継続改善)

E7 完了後の v5.0.x マイナーリリースで継続改善:

- HSM / KMS 連携の本格導入 (§13.9 の (a) オプション)
- HPKE auth mode の評価 (§14.2 推奨)
- パフォーマンス最適化 (Worker 並列化、IndexedDB チューニング)
- メアド E2EE 化の再評価 (Q13、§1)
- 多言語化 (E1-E7 では英語 / 日本語のみ)

これらは v5.0 正式版以降の継続的な取り組み。

---

## 17. 次のステップ

1. 本書のレビュー・合意形成
2. E0 プロトタイプ:
   - WebCrypto AES-GCM サンプル実装
   - WebAuthn PRF テスト (Chrome / Firefox / Safari の対応状況確認)
   - `pynacl` vs `cryptography` のベンチマーク (大量仕訳の復号速度)
3. Epic Issue #84 の本文を本書の内容で更新
4. 個別 Phase Issue (E1, E2, ...) を立てる

---

> このドキュメントは v5.0 開発開始時点の初版たたき台。E0-E1 進行中に
> 大幅修正されることを前提とする。
