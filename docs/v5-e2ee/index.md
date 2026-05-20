---
layout: default
title: v5.0 E2EE 設計書 (たたき台)
---

# v5.0 E2EE 設計書 — Epic [#84](https://github.com/nananek/iikanji-kakeibo/issues/84)

本書は v5.0 で「サーバが平文を一切持たない」E2EE 構成へ移行するための
**初版たたき台**。確定方針と未解決事項を区別して記述する。

---

## 確定事項 (2026-05-20)

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
- ユーザーの登録メール (法務連絡用、最低限必要)

### 守らないもの

- メタ情報: `created_at`, `last_login_at`, アクセスログ
- ユーザー ID (sequence)
- 暗号文のサイズ (~ 仕訳件数や仕訳の複雑さは漏れる)
- アクセスパターン (どの仕訳をいつ取得したか)

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
wrapped_keys テーブル (擬似):
| id | user_id | method | wrapped_master_key | salt | created_at |
| 1  | 42      | passkey_prf_<credid> | (32 bytes ciphertext) | NULL | ... |
| 2  | 42      | passphrase           | (32 bytes ciphertext) | salt | ... |
| 3  | 42      | recovery_seed        | (32 bytes ciphertext) | salt | ... |
```

### Passkey PRF からの鍵派生

- WebAuthn 認証時に `extensions.prf.eval.first = "iikanji-master-key-v1"` を指定
- ブラウザ / 認証器が決定論的に 32 バイトの PRF 出力を返す
- これを HKDF で派生して `derived_key_passkey` とする
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

- `users.id`, `users.email`, `users.created_at`
- 各テーブルの `id` (連番)
- 各テーブルの `created_at`, `updated_at` (タイムスタンプ)
- `user_id` 外部キー (テナント分離のため必須)
- `file_hash` (SHA-256、内容を直接漏らさない一意性キー)

### 検索可能性

- **日付・金額での絞り込み検索は不可** (全暗号化のため)
- クライアント側で全件取得して JS / Python で絞り込み
- 大量データのユーザーは UI 体感が遅くなる → ページネーション + 直近 N 件先読み戦略
- レポート (P/L, B/S) はクライアントで全件取得 → 集計

---

## 5. クライアント実装方針

### Web (WebCrypto API + Alpine.js / Vue)

- 暗号化処理は `static/js/crypto.js` (新規) に集約
- マスター鍵は **メモリ上のみ保持** (`window.IIKANJI_MK = new Uint8Array(32)`、リロード時に再認証)
- Service Worker キャッシュには暗号文しか入らない
- IndexedDB に **暗号文** のみ保存可 (オフライン対応)

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

### 鍵ラップ方式

- 監査者は登録時に自分の **公開鍵 / 秘密鍵** ペアを生成 (X25519 推奨)
- 被監査者 (owner) が監査者 (auditor) に AuditGrant を発行するとき:
  1. owner が自分のマスター鍵を auditor の公開鍵で暗号化 (=「鍵ラップ」)
  2. ラップされた鍵を `audit_grant_keys` テーブルに保存
  3. auditor がログイン時に自分の秘密鍵でアンラップ → owner の MK を取得 → owner のデータを復号

### 権限取り消し

- `AuditGrant.revoked_at` をセットしても、auditor は過去に取得した平文を持っている可能性
- 取り消し時は owner が **マスター鍵をローテーション** (= 全データを新しい鍵で再暗号化) する必要
- 再暗号化は重いバッチ処理 → 取り消し時の UX 警告

### Lv1/Lv2/Lv3 の扱い

- Lv1 (集計のみ閲覧) → owner のレポート結果だけ暗号化して提供? or 仕訳全件渡してクライアントで集計
- Lv2 (科目限定) → 暗号化レベルで分離困難 → クライアント側で全件取得後にフィルタ + サーバ側で不可視科目の id を渡さない
- Lv3 (全権限) → owner と同等の鍵アクセス

### 詳細は別途設計

監査者 UX は E5 フェーズで詳細検討。

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
| Q4 | Lv1/Lv2 監査者の暗号設計 | E5 で詳細 |
| Q5 | バックアップ・整合性監査の運用変更 (平文を見られないので運用者は監視できない) | E6 で運用ドキュメント更新 |
| Q6 | エクスポート機能 (zip ダウンロード) のバックグラウンドジョブ実装 | E6 で実装 |
| Q7 | クライアント間 (Web / Python / TUI) の暗号スキーム互換性テスト | E6 で結合テスト |
| Q8 | 移行時のサーバ生成鍵の保持期間と運用 | E7 で精緻化 |

---

## 10. 次のステップ

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
