---
layout: default
title: 証憑 API
---

# 証憑 API

証憑（レシート・領収書画像）の一覧・画像取得・ハッシュ検証・操作ログを取得する API です。

すべてのエンドポイントに `journals:read` スコープが必要です。

---

## 証憑一覧

<div class="endpoint">
  <span class="method method-get">GET</span>
  <span class="path">/api/v1/vouchers</span>
  <div class="scope">スコープ: <code>journals:read</code></div>
</div>

証憑一覧を取得します。`uploaded_at`（アップロード日時）の降順でソートされます。

> **E2EE 化に伴う変更**: 仕訳の日付・摘要・金額はサーバーで暗号化されているため、本 API での日付・摘要・金額による絞り込み（`date_from`/`date_to`/`search`/`amount_from`/`amount_to`）とレスポンスの仕訳情報（`journal`）は撤去されました。電帳法の検索要件（日付・金額・取引先）はクライアント側で復号データを検索して満たします。本 Bearer API は証憑メタのみを返します。

### クエリパラメータ

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|:---------:|------|
| `page` | integer | 1 | ページ番号 |
| `per_page` | integer | 20 | 1ページあたりの件数（上限: 100） |

### レスポンス

**200 OK**

```json
{
  "ok": true,
  "vouchers": [
    {
      "id": 5,
      "journal_entry_id": 42,
      "aad_id": "9876543210",
      "uploaded_at": "2026-02-15T10:30:00"
    }
  ],
  "total": 10,
  "page": 1,
  "per_page": 20
}
```

#### `vouchers` の各要素

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 証憑 ID |
| `journal_entry_id` | integer \| null | 紐づく仕訳 ID（孤立証憑は `null`） |
| `aad_id` | string \| null | 画像/サムネ復号の AAD 束縛用安定識別子（63bit のため文字列。平文レガシー証憑は `null`） |
| `uploaded_at` | string | アップロード日時（ISO 8601） |

---

## 証憑画像

<div class="endpoint">
  <span class="method method-get">GET</span>
  <span class="path">/api/v1/vouchers/:id/image</span>
  <div class="scope">スコープ: <code>journals:read</code></div>
</div>

証憑の画像データを取得します。

### パスパラメータ

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 証憑 ID |

### レスポンス

**200 OK** — 画像のバイナリデータ（Content-Type は証憑の MIME タイプ）

### エラー

| ステータス | メッセージ |
|:---------:|-----------|
| 404 | `証憑が見つかりません。` |
| 404 | `画像ファイルが見つかりません。` |

---

## ハッシュ検証

<div class="endpoint">
  <span class="method method-get">GET</span>
  <span class="path">/api/v1/vouchers/:id/verify</span>
  <div class="scope">スコープ: <code>journals:read</code></div>
</div>

証憑画像の SHA-256 ハッシュを再計算し、保存時のハッシュと比較します。改ざん検出に使用します。

### パスパラメータ

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 証憑 ID |

### レスポンス

**200 OK**

```json
{
  "ok": true,
  "verified": true,
  "stored_hash": "a1b2c3d4...",
  "computed_hash": "a1b2c3d4..."
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `verified` | boolean \| null | `true`: 一致、`false`: 不一致（改ざんの可能性）、`null`: ハッシュ未記録 |
| `stored_hash` | string | 保存時の SHA-256 ハッシュ |
| `computed_hash` | string | 再計算したハッシュ |

ハッシュ未記録の場合:

```json
{
  "ok": true,
  "verified": null,
  "message": "ハッシュ未記録"
}
```

### エラー

| ステータス | メッセージ |
|:---------:|-----------|
| 404 | `証憑が見つかりません。` |
| 404 | `画像ファイルが見つかりません。` |

---

## 操作ログ

<div class="endpoint">
  <span class="method method-get">GET</span>
  <span class="path">/api/v1/vouchers/:id/logs</span>
  <div class="scope">スコープ: <code>journals:read</code></div>
</div>

証憑の操作ログ（改ざん防止ログ）を取得します。作成日時の降順でソートされます。

### パスパラメータ

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 証憑 ID |

### レスポンス

**200 OK**

```json
{
  "ok": true,
  "logs": [
    {
      "id": 1,
      "action": "orphaned",
      "detail": "{\"journal_entry_id\": 42, \"entry_number\": 15, \"description\": \"コンビニ購入\"}",
      "created_at": "2026-02-20T14:00:00",
      "user_id": 1
    },
    {
      "id": 2,
      "action": "hash_verified",
      "detail": null,
      "created_at": "2026-02-21T10:00:00",
      "user_id": 1
    }
  ]
}
```

#### `logs` の各要素

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | ログ ID |
| `action` | string | 操作種別（下表参照） |
| `detail` | string \| null | 操作の詳細（JSON 文字列） |
| `created_at` | string | 操作日時（ISO 8601） |
| `user_id` | integer | 操作を行ったユーザーの ID |

#### `action` の種類

| 値 | 説明 |
|----|------|
| `orphaned` | 仕訳削除により証憑が孤立化した |
| `hash_verified` | ハッシュ検証に成功した |
| `hash_mismatch` | ハッシュ不一致（改ざんの可能性） |

### エラー

| ステータス | メッセージ |
|:---------:|-----------|
| 404 | `証憑が見つかりません。` |
