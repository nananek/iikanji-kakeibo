---
layout: default
title: 仕訳 API
---

# 仕訳 API

仕訳伝票の起票・閲覧・削除を行う API です。

---

## 仕訳起票

<div class="endpoint">
  <span class="method method-post">POST</span>
  <span class="path">/api/v1/journals</span>
  <div class="scope">スコープ: <code>journals:create</code></div>
</div>

仕訳を起票します。借方合計と貸方合計が一致する必要があります（複式簿記）。

### リクエストボディ

```json
{
  "date": "2026-02-15",
  "description": "スーパーで食材購入",
  "lines": [
    { "account_id": 12, "debit": 3000 },
    { "account_id": 1, "credit": 3000 }
  ],
  "source": "api",
  "draft_id": 10
}
```

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|:---:|------|
| `date` | string | Yes | 日付（`YYYY-MM-DD` 形式） |
| `description` | string | Yes | 摘要（空文字不可） |
| `lines` | array | Yes | 仕訳明細行の配列（1行以上） |
| `source` | string | No | ソース種別（デフォルト: `"api"`） |
| `draft_id` | integer | No | 確定する下書き ID。指定すると下書きの status が `"done"` になる |

#### `lines` の各要素

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|:---:|------|
| `account_id` | integer | Yes | 勘定科目 ID |
| `debit` | integer | No | 借方金額（デフォルト: 0） |
| `credit` | integer | No | 貸方金額（デフォルト: 0） |
| `description` | string | No | 行レベルの摘要 |

### レスポンス

**201 Created**

```json
{
  "ok": true,
  "id": 42,
  "entry_number": 15,
  "draft_id": 10
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 作成された仕訳の内部 ID |
| `entry_number` | integer | ユーザー内で一意の伝票番号 |
| `draft_id` | integer | 確定した下書き ID（`draft_id` を指定した場合のみ） |

### エラー

| ステータス | メッセージ | 原因 |
|:---------:|-----------|------|
| 400 | `date は必須です。` | 日付が未指定 |
| 400 | `description は必須です。` | 摘要が空 |
| 400 | `lines は必須です（配列）。` | 明細行が未指定 |
| 400 | `date の形式が不正です（YYYY-MM-DD）。` | 日付パースエラー |
| 400 | `lines[N].account_id は必須です。` | 科目 ID 未指定 |
| 400 | `draft_id は整数で指定してください。` | 不正な draft_id |
| 400 | `下書き(id=N)が見つからないか、既に確定済みです。` | 無効な draft_id |
| 400 | 確定済み期間チェックのエラー | 確定済み月の仕訳は追加不可 |
| 400 | `提出済みの税務科目を含むため登録できません。` | 監査提出ロック |
| 400 | 貸借不一致 | 借方合計 ≠ 貸方合計 |

---

## 仕訳一覧

<div class="endpoint">
  <span class="method method-get">GET</span>
  <span class="path">/api/v1/journals</span>
  <div class="scope">スコープ: <code>journals:read</code></div>
</div>

仕訳一覧を取得します。日付降順・伝票番号降順でソートされます。

### クエリパラメータ

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|:---------:|------|
| `date_from` | string | — | 日付の下限（`YYYY-MM-DD`、含む） |
| `date_to` | string | — | 日付の上限（`YYYY-MM-DD`、含む） |
| `page` | integer | 1 | ページ番号 |
| `per_page` | integer | 20 | 1ページあたりの件数（上限: 100） |

### レスポンス

**200 OK**

```json
{
  "ok": true,
  "journals": [
    {
      "id": 42,
      "date": "2026-02-15",
      "entry_number": 15,
      "description": "スーパーで食材購入",
      "source": "api",
      "lines": [
        { "account_id": 12, "debit": 3000, "credit": 0, "description": "" },
        { "account_id": 1, "debit": 0, "credit": 3000, "description": "" }
      ]
    }
  ],
  "total": 150,
  "page": 1,
  "per_page": 20
}
```

#### `journals` の各要素

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 仕訳 ID |
| `date` | string | 日付（`YYYY-MM-DD`） |
| `entry_number` | integer | 伝票番号 |
| `description` | string | 摘要 |
| `source` | string | ソース種別（`"journal"`, `"cashbook"`, `"api"`, `"csv"`, `"ofx"`, `"web"`, `"ai_receipt"`） |
| `lines` | array | 明細行の配列 |

---

## 仕訳詳細

<div class="endpoint">
  <span class="method method-get">GET</span>
  <span class="path">/api/v1/journals/:id</span>
  <div class="scope">スコープ: <code>journals:read</code></div>
</div>

仕訳を 1 件取得します。

### パスパラメータ

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 仕訳 ID |

### レスポンス

**200 OK**

```json
{
  "ok": true,
  "journal": {
    "id": 42,
    "date": "2026-02-15",
    "entry_number": 15,
    "description": "スーパーで食材購入",
    "source": "api",
    "lines": [...]
  }
}
```

### エラー

| ステータス | メッセージ |
|:---------:|-----------|
| 404 | `仕訳が見つかりません。` |

---

## 仕訳削除

<div class="endpoint">
  <span class="method method-delete">DELETE</span>
  <span class="path">/api/v1/journals/:id</span>
  <div class="scope">スコープ: <code>journals:delete</code></div>
</div>

仕訳を削除します。

### パスパラメータ

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `id` | integer | 仕訳 ID |

### レスポンス

**200 OK**

```json
{
  "ok": true
}
```

### エラー

| ステータス | メッセージ | 原因 |
|:---------:|-----------|------|
| 404 | `仕訳が見つかりません。` | 指定 ID の仕訳が存在しない |
| 400 | `提出済みの税務科目を含む伝票のため削除できません。` | 監査提出ロック |
| 400 | 確定済み期間チェックのエラー | 確定済み月の仕訳は削除不可 |
