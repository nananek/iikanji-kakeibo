# E7 一斉移行 運用手順書 (v4.0.0 → v5.0 E2EE)

実運用 v4.0.0 データでの検証 (#114) で判明した重要事実と、データを失わずに E2EE へ
移行するための手順を定める。設計書 §16 (一斉移行) の具体化・実態反映版。

> **このドキュメントは運用 Claude Code (本番デプロイ上) への指示書です。**
> サーバ側作業 (バックアップ・マイグレーション・genkey・検証) は Claude Code が
> 実行できますが、**ブラウザ再ラップ (§B) は人間が Web UI で行う**必要があります
> (MK は鍵設定ウィザードで人間が入力)。役割を §A (ops) / §B (人間) / §C (ops) で
> 分けて記述する。

> **単独利用者 (solo) の場合の簡略化:** 利用者が自分一人なら、§16.5/§16.6 の
> 一斉移行スキャフォールド (事前メール通知・鍵設定猶予・30日ロック
> `migration-lock-stale`・60日自動退会 `migration-purge-locked`・deprecation
> バナー) は**一切不要**。自分がログインして再ラップ → finalize すれば完了。
> これらの CLI/通知は将来の公開 (不特定多数) 時にのみ使う。

> ⚠️ **必ず使い捨てのコピー (本番ダンプを別環境にリストア) で全工程を通してから
> 本番に適用すること。** 平文 DROP・平文画像削除・finalize はいずれも不可逆。

## 前提 (なぜ単純な `flask db upgrade` ではダメか)

alembic 046→068 は**スキーマ操作のみ**で、平文→暗号文の**データ暗号化を行わない**。
055 / 057 / 060 / 068 は平文列を物理 DROP する。したがって v4.0.0 (平文のみ、暗号文
列は空) に対して `flask db upgrade` を一気に流すと、**平文を暗号化しないまま DROP し
内容を恒久的に失う**。

これを防ぐため:

1. 破壊的ドロップ migration (055/057/060/068) には**ガード** (#114) があり、暗号文が
   空のまま平文が残る行を検出すると `RuntimeError` で中断する (PR #362)。
2. 平文を暗号化する **`flask migrate-e2ee-data`** コマンド (#114) を、ドロップの前に
   実行する。

## §A. サーバ側移行手順 (運用 Claude Code が実行 / メンテナンスウィンドウ)

> 事前確認: `ai_drafts` に平文画像が残る analyzed 下書きがある場合、`migrate-e2ee-data`
> は ai_drafts 画像を暗号化しない (follow-up 未対応・ガード対象外)。移行前に下書きを
> 仕訳登録するか破棄して空にしておくこと (`SELECT count(*) FROM ai_drafts WHERE
> status='analyzed';` が 0 が望ましい)。

`migrate-e2ee-data` は冪等・リビジョン対応で、**平文列が残っているテーブル群だけ**を
暗号化する。仕訳/医療費は revision 054 (平文残存) で、証憑は revision 056
(encrypted_meta_blob/aad_id 追加・original_filename/image_mime 残存) で暗号化できる。
このため移行は **2 パス**で行う:

```bash
# 0. 必ず DB の完全バックアップを取得 (DROP・平文画像削除は不可逆)
pg_dump ... > backup_before_e2ee.sql

# 1. revision 054 まで上げる (仕訳/医療費の暗号文列・is_closing 追加、平文残存)
flask db upgrade 054_e3f_add_closing_month

# 2. パス1: 仕訳/仕訳明細/医療費を temp-MK 暗号化 (encrypted_blob へ)
flask migrate-e2ee-data

# 3. revision 056 まで上げる (055 が仕訳/医療費平文を DROP、056 が証憑の暗号文列 +
#    aad_id を追加。original_filename / image_mime はまだ残る)
flask db upgrade 056_e4_voucher_encrypted_columns

# 4. パス2: 証憑 (画像本体/サムネ/メタ/監査ログ) を temp-MK 暗号化。
#    平文画像はストレージで暗号文 (.bin) に置換し、旧平文 (本体 + 旧サムネ) を削除。
flask migrate-e2ee-data

# 5. 残りの破壊的ドロップを含めて head (現在 073) まで上げる (057 original_filename
#    DROP / 060 image_mime DROP / 068 仕訳明細列 DROP。暗号化済なのでガードを通過)。
#    068 は #338 の平文列物理 DROP を含む = ここで「DB 平文ゼロ」に到達する。
#    070-073 は #385 (ログイン派生 MK / リカバリリセット / TOTP / passkey_only 廃止) の列を
#    追加・DROP する (下記「#385 ログイン派生 MK」を必ず先に確認)。
flask db upgrade

# 6. genkey 状態を確認 (全 personal ユーザーが temp-MK を保持しているはず)
flask migration-status            # temp_mk_active が利用者数と一致することを確認
```

### §A-2. #385 ログイン派生 MK の有効化 (head=073 への追補)

#385 (単一パスワードで E2EE) はログインパスワードを MK の常用解錠鍵にする。これを有効化
するには、データ移行 (上記 1〜6) とは別に以下が必要:

```bash
# (a) LOGIN_SERVER_SECRET を本番環境変数に設定する (必須・本用途専用の固定ランダム値)。
#     未設定だと 2 ラウンドログイン API が 503 を返し login 派生方式が動かない。
#     SECRET_KEY や email 擬名化 secret とは別の独立した変数にすること。
#     例: export LOGIN_SERVER_SECRET=$(openssl rand -hex 32)
#
# (b) マイグレ 070-073 は上記 step 5 の `flask db upgrade` で適用される:
#     070 login_* 列追加 + password_hash nullable化 / 071 recovery_seed_server_hash +
#     session_token_version / 072 totp_* + totp_backup_codes / 073 passkey_only_login DROP。
#
# (c) ⚠️ 073 (passkey_only_login 物理 DROP) の事前確認: パスキー専用モードのユーザーで
#     password_hash を持つ者は、列 DROP 後にパスワードログインが復活する (意図的)。
#     把握のため適用前に件数を確認すること:
psql ... -c "SELECT COUNT(*) FROM users WHERE passkey_only_login=TRUE AND password_hash IS NOT NULL;"
#     パスワードを持たない passkey_only ユーザーは Passkey / リカバリシードでログインし、
#     リカバリシードリセットでパスワードを設定できる (移行ガイド参照)。
```

- (a) 設定後、各 v4 ユーザーは**初回ログイン時にパスワード 1 回入力で認証因子が透過移行**する
  (werkzeug 最終検証 → login_* 確立 → §B の再ラップへ)。ops 側の per-user 操作は不要。
- TOTP 2 要素認証は利用者が任意で有効化する (ops 作業なし)。

> 注意: 本番コンテナの `entrypoint.sh` は起動時に `flask db upgrade` を head まで
> 自動実行する。genkey 前に通常起動すると 055 のガードで停止する (平文保護)。
> 移行時は web を通常起動せず、上記 1〜6 を**手動で順に**実行すること。

各パスを飛ばすと、対応する DROP migration のガードが中断し平文は保護される
(トランザクション巻き戻し)。`migrate-e2ee-data` は暗号化済の行をスキップするので
再実行は安全。

> ⚠️ パス1 (仕訳/医療費) は revision 054、パス2 (証憑) は revision 056 でのみ有効
> (平文列を raw SQL で読むため)。対応する DROP が済んだ後は平文が失われており実行不可。
> ⚠️ AI 下書き画像 (ai_drafts) の移行は本コマンド未対応 (follow-up)。ただし ai_drafts に
> 平文画像が残るデプロイでは、別途暗号化が必要 (現状ガード対象外)。

## §B. ブラウザ再ラップ (人間が Web UI で実行 / E2EE 完成)

§A で暗号化したデータは**サーバが知る一時鍵 (temp-MK)** で暗号化されている。これは
移行期間中の暫定状態 (§16.4) で、まだ真の E2EE ではない (サーバが復号可能)。各利用者が
ブラウザで自分の本物 MK へ**再ラップ**して初めて E2EE が確立する。実装済み (PR-4b〜4d、
ブラウザ E2E `tests/e2e/migration-rewrap.spec.ts` で検証済)。手順:

1. v5.0 サービスを通常起動し、対象利用者が Web にログインする。
2. **暗号鍵を設定**: 「設定 → 暗号鍵管理」のウィザードで方式を選び MK を設定
   (パスフレーズ / Passkey PRF / リカバリシード 24 単語)。完了で MK が SharedWorker に
   解錠され、X25519 公開鍵 (`users.public_key`) が確立する。**この MK を忘れると復元
   不能** (サーバは保持しない) — リカバリシードは必ず紙で保管。
3. **ダッシュボード上部の「E2EE 移行を完了する」バナー**の「再ラップ移行を開始」を
   クリック。`runRewrapMigration` が以下を temp-MK 復号 → 本物 MK 再暗号化で順に再ラップ:
   - 仕訳 (je) / 仕訳明細 (jel) — 年度ごと
   - 医療費 (me) / 残高キャッシュ (bcb)
   - **証憑画像 (vimg) / サムネ (vthumb) / メタ (vmeta) / 監査ログ (valog)** — 件数が
     多いと時間がかかる (実データ 332 枚で検証済)。AAD は不変、鍵と IV のみ変わる。
   - 完了後 `POST /api/v1/migration/finalize` → サーバが `migration_temp_mk` を NULL
     クリア。進捗バー 100% でページがリロードしバナーが消える。
4. **中断耐性**: 失敗・中断したら再クリックで継続する (temp-MK で復号できない=再ラップ
   済みとみなしスキップ、冪等)。鍵未解錠ならバナーが解錠を促す。

> solo の場合はここまでで実質完了。複数利用者なら全員がこの手順を終えるまで temp-MK は
> 廃棄しない。猶予を過ぎた未設定者のロック/退会が必要になったら §16.5 の CLI
> (`migration-lock-stale` / `migration-purge-locked`、いずれも dry-run 既定) を使う。

## §C. 移行完了の検証と temp-MK 廃棄 (運用 Claude Code)

```bash
# 全員の再ラップ完了を確認 (temp_mk_active=0 / safe_to_discard_temp_mk=true)
flask migration-status --json

# (任意) 運用ダッシュボードでも確認可能。OPS_BASIC_AUTH_USER/_PASS 設定時:
#   GET /admin/migration-progress(.json) → temp_mk_finalize_eligible=true
```

- `temp_mk_active=0` なら全データが本物 MK 化済み = **真の E2EE 確立**。サーバはもう
  どの利用者のデータも復号できない。
- アプリ健全性: 仕訳帳・元帳・レポート・証憑画像がブラウザで正しく表示される
  (= 本物 MK でクライアント復号できている) ことを確認。
- (任意) `SECRET_KEY` (旧 Fernet 用) のローテーション: E2 で API キーはクライアント
  暗号化へ移行済みのため機微データには未使用だが、念のため新規生成して入れ替えてよい。

## ロールバック / 障害対応

- **E2EE 化後はスキーマだけ戻してもデータは復号不能** (`flask db downgrade` は補助的)。
  復旧の本命は **§A 手順 0 で取得した DB バックアップ + ストレージ snapshot からの完全
  復元**。移行前に「バックアップから別環境へ復元できる」ことを必ず事前検証しておく。
- §A の各パスを飛ばしても DROP ガードがトランザクションを巻き戻すので、平文は保護される
  (途中失敗時はバックアップ復元 or ガード位置から再開)。

## 互換性の根拠 (永久復号不能を避ける)

サーバ側暗号化 (`app/services/migration_crypto.py`) はクライアント (Web `record.js` /
client-py `iikanji.crypto`) と**バイト互換**:

- AES-256-GCM (IV 12B / tag 16B)、MK を直接 GCM 鍵に使用 (HKDF なし)
- 平文 = `json.dumps(record, separators=(",",":"), ensure_ascii=False)` の UTF-8
- AAD (Option B) = `tableType + 0x00 + uint64_be(user_id)` (je/jel/me は追加 id なし)

`tests/test_migration_crypto.py` が JS 由来の golden vector でバイト一致を検証。実
v4.0.0 データ (使い捨て DB) で「暗号化 → temp-MK 復号 → 平文一致 (842/1758/3 件、
借方=貸方=10,964,736)」と「client-py 独立実装での復号一致」を確認済。

## 実装済み / 未実装 (本 runbook 時点)

| 項目 | 状態 |
|---|---|
| 破壊的ドロップのガード (055/057/060/068) | ✅ #362 |
| `migration_crypto` (互換暗号プリミティブ・blob/record) | ✅ |
| `migrate-e2ee-data` (仕訳/明細/医療費) | ✅ |
| 証憑画像・サムネ・メタ (vmeta/vimg/vthumb) の暗号化 + 旧平文画像削除 | ✅ |
| voucher_audit_logs.detail (valog) の暗号化 | ✅ |
| 移行手術: 056 に aad_id 前倒し・058 冪等化 (original_filename と aad_id 共存) | ✅ |
| AI 下書き画像 (ai_drafts) の `migrate-e2ee-data` 暗号化 | ⬜ 未 (follow-up。移行前に下書きを空にして回避) |
| クライアント側 temp-MK 再ラップフロー (E2EE 完成) | ✅ PR-4b 系 (#365-369) / バナー hook / rewrap_flow |
| ブラウザ E2E (再ラップ→finalize→復号一致) | ✅ #375 `tests/e2e/migration-rewrap.spec.ts` |
| §16.5 鍵未設定ロック (lock-stale/purge-locked, 公開時用) | ✅ #371-373 (solo では不要) |
| §16.6 進捗ダッシュボード `/admin/migration-progress` | ✅ #374 (Basic 認証) |
| balance_cache_blobs の再ラップ (bcb) | ✅ rewrap_flow が年度ごとに処理 |
| #385 ログイン派生 MK (070) / リカバリリセット (071) / TOTP (072) / passkey_only DROP (073) | ✅ #391-411 (LOGIN_SERVER_SECRET 設定要・§A-2 参照) |
| client-py byte 互換 (login 派生 / recovery_verifier) | ✅ client-py #17 (v3.1.0) |
| マイグレーション head | 073 (068 = #338 平文列 DROP / 073 = passkey_only DROP) |

実 v4.0.0 データ (使い捨て PG18 + 証憑 332 枚) で **head(068) までの完全移行を検証済**:
仕訳842/明細1758/医療費3/証憑332 を暗号化 → 057/060/068 ガード通過 → 平文列 DROP +
平文画像/旧サムネ全削除 (0 残) → client-py 独立実装で画像/メタを復号して元データ一致
(file_hash_plain 一致・JPEG マジック確認)。
