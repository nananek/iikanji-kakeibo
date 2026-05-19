---
layout: default
title: バックアップと復旧
---

# バックアップと復旧

公開 SaaS 運用での障害対応のため、以下 3 種類のデータをバックアップする
必要がある。

| データ | 媒体 | 復旧優先度 |
|--------|------|----------|
| PostgreSQL DB (仕訳・科目・ユーザー・認証情報すべて) | DB ダンプ | 最高 |
| 証憑画像 + サムネイル | S3 互換ストレージ or ローカルファイル | 高 |
| アプリケーションログ (退会証跡・voucher 削除ログ) | ファイルログ | 中 |

電帳法スキャナ保存の証憑関連は **7 年保管義務** があるため、退会後も
匿名化保持される `VoucherAuditLog` と画像本体は確実に長期保管する。

---

## 1. PostgreSQL ダンプ

### 日次ダンプの自動化 (cron 例)

```bash
#!/bin/bash
# /etc/cron.daily/iikanji-backup
set -euo pipefail

BACKUP_DIR=/var/backups/iikanji
DATE=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR"

# 圧縮ダンプ (custom format、リストア時に並列処理可)
docker exec server-db-1 pg_dump \
  -U iikanji -d iikanji -F c \
  --no-owner --no-acl \
  > "$BACKUP_DIR/iikanji-$DATE.dump"

# 30 日経過分を削除
find "$BACKUP_DIR" -name 'iikanji-*.dump' -mtime +30 -delete
```

### 復旧

```bash
# 既存 DB を完全に置き換える場合 (要 docker compose down + drop database)
docker exec -i server-db-1 pg_restore \
  -U iikanji -d iikanji -c \
  < /path/to/iikanji-YYYYMMDDTHHMMSSZ.dump
```

部分復旧 (例: 特定テーブルのみ) は `pg_restore -t <table_name>` を使う。

---

## 2. 証憑画像のバックアップ

### S3 互換ストレージを使用している場合 (`STORAGE_BACKEND=s3`)

S3 側のレプリケーション機能 (AWS S3 Cross-Region Replication, Wasabi
Snapshot 等) を有効化することを推奨。アプリケーションは
`STORAGE_S3_BUCKET` / `STORAGE_S3_ENDPOINT_URL` を参照するため、
復旧時はこれらを新しいバケット URL に切り替えればよい。

### ローカルファイルを使用している場合 (`STORAGE_BACKEND=local`)

```bash
# 日次差分バックアップ (rsync)
rsync -av --delete \
  /var/lib/docker/volumes/iikanji_voucher_data/_data/ \
  user@backup-host:/var/backups/iikanji-vouchers/
```

復旧:
```bash
# バックアップから volume にリストア
rsync -av \
  user@backup-host:/var/backups/iikanji-vouchers/ \
  /var/lib/docker/volumes/iikanji_voucher_data/_data/
```

復旧後は **整合性監査バッチ** を実行して `StorageUsage` と実体ファイル
のドリフトを検出する:
```bash
docker exec -w /app server-web-1 flask storage-audit --fix
```

---

## 3. アプリケーションログ

`logger.warning("voucher deleted: ...")` 等の運用ログは Docker の標準
出力に流れる。長期保存するには:

### Docker logging driver 経由

```yaml
# docker-compose.yml
services:
  web:
    logging:
      driver: "json-file"
      options:
        max-size: "100m"
        max-file: "30"  # 30 日分保持
```

### または syslog / journald 集約

Tailscale 経由運用の場合、`syslog-ng` や `vector` でログを集約サーバー
に転送する設定例は今後追加予定。

---

## 整合性監査 (Phase 5 #70)

DB と StorageUsage の差分を検出する CLI:

```bash
# Dry-run: drift を検出してレポート
docker exec -w /app server-web-1 flask storage-audit

# 修正: file_size NULL backfill + drift 解消
docker exec -w /app server-web-1 flask storage-audit --fix
```

定期実行を推奨 (cron で日次 dry-run + 月次 fix)。

---

## 災害復旧チェックリスト

1. [ ] 最新 `pg_dump` の存在確認 (24 時間以内)
2. [ ] 証憑ストレージのバックアップ (S3 レプリカ or rsync) の存在確認
3. [ ] `docker compose up -d` で起動できる Compose ファイル一式
4. [ ] `.env` (SECRET_KEY / SMTP / OPERATOR_* / DB パスワード)
5. [ ] DNS / Tailscale 設定
6. [ ] 復旧手順 (`pg_restore` + 画像 rsync + `flask storage-audit --fix`)

`.env` を Git にコミットしないため、別途オフライン媒体での保管が
必須。秘密鍵管理 (HashiCorp Vault, 1Password 等) の利用を推奨。
