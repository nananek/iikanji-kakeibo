---
layout: default
title: REST API 概要
---

# REST API 概要

いいかんじ™家計簿は、仕訳の起票・閲覧・削除、AI 証憑仕訳を操作できる REST API を提供しています。

## ベース URL

```
https://your-server.example.com/api/v1
```

## 認証

すべての API リクエストに `Authorization` ヘッダーが必要です。

```
Authorization: Bearer ik_xxxxxxx
```

API キーはサーバーの **設定 > API キー管理** から発行できます。キーは `ik_` プレフィックスで始まります。

## スコープ

API キー発行時にスコープ（権限）を選択します。

| スコープ | 説明 | 使えるエンドポイント |
|---------|------|-------------------|
| `journals:create` | 仕訳起票 | `POST /journals` |
| `journals:read` | 仕訳閲覧 | `GET /journals`, `GET /journals/:id` |
| `journals:delete` | 仕訳削除 | `DELETE /journals/:id` |
| `ai:analyze` | AI 証憑仕訳 | `POST /ai/analyze`, `GET/DELETE /ai/drafts` |

`journals:delete` は `journals:read` スコープを前提とします（削除対象を閲覧できる必要があるため）。

## 共通レスポンス形式

### 成功時

すべての成功レスポンスに `"ok": true` が含まれます。

```json
{
  "ok": true,
  "id": 42,
  "entry_number": 15
}
```

### エラー時

エラーレスポンスには `"error"` フィールドに日本語のメッセージが含まれます。

```json
{
  "error": "date は必須です。"
}
```

## エラーコード

| HTTP ステータス | 意味 | 例 |
|:-:|------|-----|
| 400 | バリデーションエラー | 必須フィールド不足、不正な日付形式、確定済み期間 |
| 401 | 認証失敗 | API キー未指定・無効 |
| 403 | 権限不足 | スコープが足りない |
| 404 | リソースなし | 指定 ID の仕訳/下書きが存在しない |
| 429 | レート制限超過 | AI 解析 API のリクエスト過多 |

## ページネーション

一覧取得 API はページネーションに対応しています。

**リクエストパラメータ:**

| パラメータ | デフォルト | 上限 | 説明 |
|-----------|:---------:|:----:|------|
| `page` | 1 | — | ページ番号 |
| `per_page` | 20 or 50 | 100 | 1ページあたりの件数 |

**レスポンス:**

```json
{
  "ok": true,
  "journals": [...],
  "total": 150,
  "page": 1,
  "per_page": 20
}
```

`total` は条件に合致する全件数です。

## レート制限

| エンドポイント | 制限 |
|--------------|------|
| `POST /api/v1/ai/analyze` | **30 リクエスト / 時間** |
| その他 | 制限なし |

制限を超えると `429 Too Many Requests` が返ります。

## エンドポイント一覧

### 仕訳 API

| メソッド | パス | 説明 | スコープ |
|---------|------|------|---------|
| POST | `/api/v1/journals` | [仕訳起票](journals.html#仕訳起票) | `journals:create` |
| GET | `/api/v1/journals` | [仕訳一覧](journals.html#仕訳一覧) | `journals:read` |
| GET | `/api/v1/journals/:id` | [仕訳詳細](journals.html#仕訳詳細) | `journals:read` |
| DELETE | `/api/v1/journals/:id` | [仕訳削除](journals.html#仕訳削除) | `journals:delete` |

### AI 証憑仕訳 API

| メソッド | パス | 説明 | スコープ |
|---------|------|------|---------|
| POST | `/api/v1/ai/analyze` | [画像解析](ai-drafts.html#画像解析) | `ai:analyze` |
| GET | `/api/v1/ai/drafts` | [下書き一覧](ai-drafts.html#下書き一覧) | `ai:analyze` |
| GET | `/api/v1/ai/drafts/:id` | [下書き詳細](ai-drafts.html#下書き詳細) | `ai:analyze` |
| DELETE | `/api/v1/ai/drafts/:id` | [下書き削除](ai-drafts.html#下書き削除) | `ai:analyze` |
