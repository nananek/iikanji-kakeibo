---
layout: default
title: 運用ガイド
---

# 運用ガイド

公開 SaaS / 自家ホスト運用者向けの運用ドキュメント。

## 目次

- [バックアップと復旧]({{ '/operations/backup/' | relative_url }}) — PostgreSQL ダンプ・S3 オブジェクト・整合性監査
- [監視]({{ '/operations/monitoring/' | relative_url }}) — ログ・エラー検知・容量警告
- [SECRET_KEY ローテーション]({{ '/operations/secret-key-rotation/' | relative_url }}) — Flask 秘密鍵の更新手順 (既存 v4.x デプロイから v5.0 アップグレード時必須、新規 v5.0 デプロイは不要)

## 主要な運用 CLI コマンド

すべて `docker exec -w /app server-web-1 flask <COMMAND>` で実行できる:

| コマンド | 用途 |
|---------|------|
| `seed` | 標準勘定科目区分の初期投入 (初回起動時に自動実行) |
| `seed-user <username>` | 指定ユーザーへ標準科目を再投入 |
| `auto-import` | 自動取込の手動実行 (WebDAV 等) |
| `generate-thumbnails` | 既存証憑画像のサムネイル一括生成 |
| `storage-audit [--fix]` | ストレージ整合性監査 (file_size backfill + drift 検出) |
| `notify-terms-update` | 規約改訂メールの一括送信 |
| `invite-create <email> [--user-type personal\|auditor] [--expires-in-days 7] [--no-email]` | 招待トークン発行 |

各コマンドのオプション詳細は `flask <COMMAND> --help` で確認できる。
