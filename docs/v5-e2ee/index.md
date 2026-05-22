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

- 全ユーザー (owner / auditor 問わず) が登録時に X25519 公開鍵 / 秘密鍵ペアを
  生成 (公開鍵は `users.public_key` に平文保管、秘密鍵は MK でラップして
  `wrapped_keys` に保管)
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
8 bit checksum (SHA-256 末尾 8bit)。クライアント側で入力時にチェックサム
検証を行い、不一致なら「入力ミスです」と即時エラー表示 (HKDF を回して
「復号失敗」と出るより親切)。これにより「シードが違う」か「入力ミス」かを
ユーザーに伝えられる。
  |                                  |
  | 4. derived_key で wrapped を unwrap |
  |    → MK を Worker クロージャに    |
  |                                  |
  | 5. PUT /api/v1/wrapped-keys/<id>/touch (last_used_at 更新) |
  |--------------------------------->|
```

- アンラップ失敗 (タグ検証 NG) は「鍵が間違っている」を示す。攻撃検知では
  ないので一般エラーで返す
- リロード時は Worker メモリが揮発するため再アンラップが必要 (§5 と §11
  Q10 整理参照)

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
- **(c) ServiceWorker 内 Web Worker でセッション維持**: ServiceWorker は
  リロードを跨いで生存。同一 origin の Web Worker 鍵をブラウザ閉じるまで保持
  可能。ただし ServiceWorker 自身が XSS で乗っ取られるリスクあり
- **(d) Page Visibility / idle 検知で自動再認証**: 一定時間操作なしで自動
  ロック (=ServiceWorker から MK を消す)。これは (c) と組合せる

→ **暫定: (c) + (d) の組合せ**。ServiceWorker 内 Web Worker で MK 保持 +
30 分 idle で自動ロック。実装は E0 プロトタイプ上で検証 (Q10)。

#### マルチタブ時の挙動

ServiceWorker は同一 origin の全タブで共有されるため、タブ A でアンラップ
した MK はタブ B でも即座に利用可能になる。これは利便性として望ましい。

idle カウントの仕様:
- **タブ単位ではなく ServiceWorker 単位で 1 つのカウンタ** を持つ
- いずれかのタブで Page Visibility が `visible` or ユーザー操作 (mouse/keyboard)
  があればカウンタリセット
- どのタブもアクティブでない状態が 30 分継続したら MK を消去
- 消去後はどのタブからの操作も再認証要求

これにより「タブ B を開いたまま放置してタブ A で操作中」のシナリオで誤って
ロックが発火する事故を防ぐ。

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
- [ ] Q9/Q10 の実装 (Bearer + MK の併用、ServiceWorker セッション維持、マルチタブ idle カウンタ共有)

E2 (API キー E2EE 化) はこの基盤を最初に使う「最小スコープ検証」。

---

## 11. 次のステップ

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
