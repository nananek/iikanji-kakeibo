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

証憑一覧を取得します。日付降順でソートされます。電帳法の検索要件（日付・金額・取引先）に対応しています。

### クエリパラメータ

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|:---------:|------|
| `date_from` | string | — | 日付の下限（`YYYY-MM-DD`、含む） |
| `date_to` | string | — | 日付の上限（`YYYY-MM-DD`、含む） |
| `amount_from` | integer | — | 金額の下限（含む） |
| `amount_to` | integer | — | 金額の上限（含む） |
| `search` | string | — | 摘要で部分一致検索 |
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
      "image_mime": "image/jpeg",
      "uploaded_at": "2026-02-15T10:30:00",
      "deadline_exceeded": false,
      "journal": {
        "date": "2026-02-15",
        "description": "コンビニ購入",
        "amount": 850
      }
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
| `image_mime` | string | 画像の MIME タイプ |
| `uploaded_at` | string | アップロード日時（ISO 8601） |
| `deadline_exceeded` | boolean | 入力期限（約2ヶ月+7日）を超過しているか |
| `journal` | object \| null | 紐づく仕訳の情報（孤立証憑は `null`） |

#### `journal`

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `date` | string | 仕訳日付（`YYYY-MM-DD`） |
| `description` | string | 摘要 |
| `amount` | integer | 借方合計金額 |

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
