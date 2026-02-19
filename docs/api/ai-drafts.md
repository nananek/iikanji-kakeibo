---
layout: default
title: AI 証憑仕訳 API
---

# AI 証憑仕訳 API

領収書・請求書等の画像を AI で解析し、仕訳候補を生成する API です。下書きとして保存され、確認後に仕訳として確定できます。

すべてのエンドポイントに `ai:analyze` スコープが必要です。サーバー側で AI API 設定（OpenAI / Gemini / Claude）が完了している必要があります。

---

## 画像解析

<div class="endpoint">
  <span class="method method-post">POST</span>
  <span class="path">/api/v1/ai/analyze</span>
  <div class="scope">スコープ: <code>ai:analyze</code> &#x7c; レート制限: <strong>30 リクエスト / 時間</strong></div>
</div>

画像を AI で解析し、仕訳候補を含む下書きを作成します。

### リクエスト

**Content-Type:** `multipart/form-data`

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|:---:|------|
| `image` | file | Yes | 画像ファイル（JPEG / PNG / WebP / GIF、上限 10MB） |
| `comment` | string | No | メモ（最大 500 文字）。「会社の懇親会」等のヒントで解析精度が向上 |
| `notify` | string | No | `"1"` を指定すると設定済みの Webhook に通知を送信 |

### レスポンス

**201 Created**

```json
{
  "ok": true,
  "draft_id": 10,
  "suggestions": [
    {
      "title": "コンビニ購入",
      "date": "2026-02-15",
      "entry_description": "セブンイレブン 食品購入",
      "lines": [
        {
          "account_id": 12,
          "account_name": "食費",
          "debit_amount": 850,
          "credit_amount": 0,
          "description": ""
        },
        {
          "account_id": 1,
          "account_name": "現金",
          "debit_amount": 0,
          "credit_amount": 850,
          "description": ""
        }
      ]
    }
  ]
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `draft_id` | integer | 作成された下書きの ID |
| `suggestions` | array | 仕訳候補のリスト（通常 1〜3 件） |

#### `suggestions` の各候補

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `title` | string | 候補のタイトル |
| `date` | string | 推定された日付（`YYYY-MM-DD`） |
| `entry_description` | string | 推定された摘要 |
| `lines` | array | 仕訳明細行 |

#### `lines` の各行

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `account_id` | integer | 勘定科目 ID |
| `account_name` | string | 勘定科目名 |
| `debit_amount` | integer | 借方金額 |
| `credit_amount` | integer | 貸方金額 |
| `description` | string | 行の摘要 |

### エラー

| ステータス | メッセージ | 原因 |
|:---------:|-----------|------|
| 400 | `AI API設定が未登録です。` | サーバーで AI 設定が未完了 |
| 400 | `image は必須です。` | 画像ファイル未添付 |
| 400 | `ファイルサイズが大きすぎます（上限10MB）。` | 10MB 超過 |
| 400 | `対応していないファイル形式です。...` | 非対応の MIME タイプ |
| 429 | — | レート制限超過 |

### 仕訳として確定する

下書きの候補を仕訳として確定するには、[仕訳起票 API](journals.html#仕訳起票) の `draft_id` パラメータを使います。

```json
POST /api/v1/journals
{
  "date": "2026-02-15",
  "description": "セブンイレブン 食品購入",
  "lines": [
    { "account_id": 12, "debit": 850 },
    { "account_id": 1, "credit": 850 }
  ],
  "draft_id": 10
}
```

`draft_id` を指定すると、仕訳起票と同時に下書きの status が `"done"` に変わります。

---

## 下書き一覧

<div class="endpoint">
  <span class="method method-get">GET</span>
  <span class="path">/api/v1/ai/drafts</span>
  <div class="scope">スコープ: <code>ai:analyze</code></div>
</div>

下書きの一覧を取得します。作成日時の降順でソートされます。

### クエリパラメータ

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|:---------:|------|
| `status` | string | `"analyzed"` | フィルタ: `"analyzed"`（未確定）/ `"done"`（確定済）/ `"all"` |
| `page` | integer | 1 | ページ番号 |
| `per_page` | integer | 50 | 1ページあたりの件数（上限: 100） |

### レスポンス

**200 OK**

```json
{
  "ok": true,
  "drafts": [
    {
      "id": 10,
      "status": "analyzed",
      "comment": "コンビニ",
      "created_at": "2026-02-15T10:30:00",
      "summary": {
        "title": "コンビニ購入",
        "date": "2026-02-15",
        "description": "セブンイレブン 食品購入",
        "amount": 850,
        "suggestion_count": 1
      }
    }
  ],
  "total": 5,
  "page": 1,
  "per_page": 50
}
```

#### 各下書き

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 下書き ID |
| `status` | string | `"analyzed"`（未確定）/ `"done"`（確定済） |
| `comment` | string | メモ |
| `created_at` | string | 作成日時（ISO 8601） |
| `summary` | object \| null | 最初の候補から生成されたサマリー |

#### `summary`

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `title` | string | 候補のタイトル |
| `date` | string | 推定日付 |
| `description` | string | 推定摘要 |
| `amount` | integer | 借方合計金額 |
| `suggestion_count` | integer | 候補数 |

---

## 下書き詳細

<div class="endpoint">
  <span class="method method-get">GET</span>
  <span class="path">/api/v1/ai/drafts/:id</span>
  <div class="scope">スコープ: <code>ai:analyze</code></div>
</div>

下書きの詳細を取得します。候補データ（`suggestions`）を含みます。

### パスパラメータ

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 下書き ID |

### レスポンス

**200 OK**

```json
{
  "ok": true,
  "draft": {
    "id": 10,
    "status": "analyzed",
    "comment": "コンビニ",
    "created_at": "2026-02-15T10:30:00",
    "summary": { ... },
    "suggestions": [
      {
        "title": "コンビニ購入",
        "date": "2026-02-15",
        "entry_description": "セブンイレブン 食品購入",
        "lines": [...]
      }
    ]
  }
}
```

`suggestions` の構造は[画像解析](#画像解析)のレスポンスと同じです。

### エラー

| ステータス | メッセージ |
|:---------:|-----------|
| 404 | `下書きが見つかりません。` |

---

## 下書き削除

<div class="endpoint">
  <span class="method method-delete">DELETE</span>
  <span class="path">/api/v1/ai/drafts/:id</span>
  <div class="scope">スコープ: <code>ai:analyze</code></div>
</div>

下書きを削除します。

### パスパラメータ

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 下書き ID |

### レスポンス

**200 OK**

```json
{
  "ok": true
}
```

### エラー

| ステータス | メッセージ |
|:---------:|-----------|
| 404 | `下書きが見つかりません。` |
