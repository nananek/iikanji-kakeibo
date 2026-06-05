# E7 一斉移行 運用手順書 (v4.0.0 → v5.0 E2EE)

実運用 v4.0.0 データでの検証 (#114) で判明した重要事実と、データを失わずに E2EE へ
移行するための手順を定める。設計書 §16 (一斉移行) の具体化・実態反映版。

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

## 移行手順 (メンテナンスウィンドウ)

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

# 5. 残りの破壊的ドロップを含めて head まで上げる (057 original_filename DROP /
#    060 image_mime DROP / 068 仕訳明細列 DROP。暗号化済なのでガードを通過する)
flask db upgrade
```

各パスを飛ばすと、対応する DROP migration のガードが中断し平文は保護される
(トランザクション巻き戻し)。`migrate-e2ee-data` は暗号化済の行をスキップするので
再実行は安全。

> ⚠️ パス1 (仕訳/医療費) は revision 054、パス2 (証憑) は revision 056 でのみ有効
> (平文列を raw SQL で読むため)。対応する DROP が済んだ後は平文が失われており実行不可。
> ⚠️ AI 下書き画像 (ai_drafts) の移行は本コマンド未対応 (follow-up)。ただし ai_drafts に
> 平文画像が残るデプロイでは、別途暗号化が必要 (現状ガード対象外)。

## temp-MK と E2EE 完成 (移行後)

手順 2 で暗号化したデータは**サーバが知る一時鍵 (temp-MK)** で暗号化されている。これは
移行期間中の暫定状態 (§16.4) であり、まだ真の E2EE ではない (サーバが復号可能)。

各利用者がログイン後、クライアントで:

1. `users.migration_temp_mk` を取得し、自分のデータを temp-MK で復号、
2. 自分の本物の MK (Passkey / パスフレーズ / リカバリシード由来) で再暗号化して
   アップロード、
3. サーバが `encrypted_blob` を上書きし `migration_temp_mk` を NULL クリアする。

この**クライアント側再ラップフロー**の完了をもって E2EE が確立する。

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
| AI 下書き画像 (ai_drafts) の暗号化 | ⬜ 未 (follow-up。ガード対象外) |
| クライアント側 temp-MK 再ラップフロー (E2EE 完成) | ⬜ 未 (E7 仕上げ) |
| balance_cache_blobs の移行 (旧 balance_caches は 053 で DROP) | 要確認 |

実 v4.0.0 データ (使い捨て PG18 + 証憑 332 枚) で **head(068) までの完全移行を検証済**:
仕訳842/明細1758/医療費3/証憑332 を暗号化 → 057/060/068 ガード通過 → 平文列 DROP +
平文画像/旧サムネ全削除 (0 残) → client-py 独立実装で画像/メタを復号して元データ一致
(file_hash_plain 一致・JPEG マジック確認)。
