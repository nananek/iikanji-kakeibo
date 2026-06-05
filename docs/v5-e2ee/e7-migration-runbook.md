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

```bash
# 0. 必ず DB の完全バックアップを取得 (DROP は不可逆)
pg_dump ... > backup_before_e2ee.sql

# 1. スキーマを revision 054 まで上げる (暗号文列・is_closing 追加、平文はまだ残る)
flask db upgrade 054_e3f_add_closing_month

# 2. 平文台帳データを temp-MK でサーバ側暗号化する (冪等)
#    仕訳 / 仕訳明細 / 医療費を encrypted_blob へ。users.migration_temp_mk に
#    利用者ごとの一時鍵を生成・保管。
flask migrate-e2ee-data

# 3. 残りの破壊的ドロップを含めて head まで上げる
#    (暗号化済みなので 055 等のガードを通過し、平文列が安全に DROP される)
flask db upgrade
```

`migrate-e2ee-data` を飛ばして手順 3 を実行した場合、055 のガードが中断し平文は
保護される (ロールバックで revision 054 以前へ戻る)。

> ⚠️ 手順 2 は **revision 054 の状態でのみ**実行できる (平文列を raw SQL で読むため)。
> 055 以降へ進んだ後は平文が失われており実行不可 (コマンドが precheck で検出して停止)。

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
| `migration_crypto` (互換暗号プリミティブ) | ✅ |
| `migrate-e2ee-data` (仕訳/明細/医療費) | ✅ |
| 証憑画像・サムネ・メタ (vmeta/vimg/vthumb) の暗号化 | ⬜ 未 (057/060 ガードが通るには必要) |
| voucher_audit_logs.detail (valog) の暗号化 | ⬜ 未 |
| AI 下書き画像の暗号化 | ⬜ 未 |
| クライアント側 temp-MK 再ラップフロー (E2EE 完成) | ⬜ 未 |
| balance_cache_blobs の移行 (旧 balance_caches は 053 で DROP) | 要確認 |

証憑・AI 画像の暗号化が未実装のため、現時点で手順 3 を head まで通すと 057 (voucher
平文 DROP) のガードで停止する。証憑系の暗号化実装までは、検証は revision 056 までで
確認する (仕訳・医療費の移行は完全に機能する)。
