---
layout: default
title: "Python クライアント: API リファレンス"
---

# API リファレンス

## KakeiboClient

メインのクライアントクラス。コンテキストマネージャ（`with` 文）に対応。

### コンストラクタ

```python
KakeiboClient(
    base_url: str,
    api_key: str,
    *,
    timeout: float = 30.0,
    http_client: httpx.Client | None = None,
)
```

| 引数 | 型 | 説明 |
|------|-----|------|
| `base_url` | `str` | サーバーの URL（例: `"https://example.com"`） |
| `api_key` | `str` | API キー（`ik_` プレフィックス付き） |
| `timeout` | `float` | HTTP タイムアウト秒数（デフォルト: 30.0） |
| `http_client` | `httpx.Client \| None` | カスタム httpx クライアント（テスト用） |

### メソッド

#### `create_journal`

仕訳を起票する。必要なスコープ: `journals:create`

```python
create_journal(
    *,
    date: date | datetime | str,
    description: str,
    lines: list[JournalLine],
    source: str = "api",
    draft_id: int | None = None,
) -> JournalCreateResponse
```

| 引数 | 型 | 説明 |
|------|-----|------|
| `date` | `date \| datetime \| str` | 日付。`date`, `datetime`, または `"YYYY-MM-DD"` 文字列 |
| `description` | `str` | 摘要（必須、空文字不可） |
| `lines` | `list[JournalLine]` | 仕訳明細行のリスト（1行以上必須） |
| `source` | `str` | ソース種別（デフォルト: `"api"`） |
| `draft_id` | `int \| None` | 確定する下書き ID（省略可）。指定すると下書きの status が done になる |

**戻り値:** `JournalCreateResponse`

#### `get_journal`

仕訳を1件取得する。必要なスコープ: `journals:read`

```python
get_journal(journal_id: int) -> JournalDetail
```

| 引数 | 型 | 説明 |
|------|-----|------|
| `journal_id` | `int` | 仕訳 ID |

**戻り値:** `JournalDetail`

#### `list_journals`

仕訳一覧を取得する。必要なスコープ: `journals:read`

```python
list_journals(
    *,
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> JournalListResponse
```

| 引数 | 型 | 説明 |
|------|-----|------|
| `date_from` | `date \| datetime \| str \| None` | 日付の下限（省略可） |
| `date_to` | `date \| datetime \| str \| None` | 日付の上限（省略可） |
| `page` | `int` | ページ番号（デフォルト: 1） |
| `per_page` | `int` | 1ページあたりの件数（デフォルト: 20, 上限: 100） |

**戻り値:** `JournalListResponse`

#### `delete_journal`

仕訳を削除する。必要なスコープ: `journals:delete`

```python
delete_journal(journal_id: int) -> None
```

| 引数 | 型 | 説明 |
|------|-----|------|
| `journal_id` | `int` | 仕訳 ID |

**例外:** 確定済み期間や提出ロック中の仕訳は削除不可（`KakeiboAPIError` 400）

#### `analyze`

画像を AI 解析して下書きを作成する。必要なスコープ: `ai:analyze`

```python
analyze(
    image: str | Path | bytes,
    *,
    comment: str = "",
    notify: bool = False,
    mime_type: str | None = None,
) -> AnalyzeResponse
```

| 引数 | 型 | 説明 |
|------|-----|------|
| `image` | `str \| Path \| bytes` | 画像ファイルパスまたはバイト列 |
| `comment` | `str` | メモ（省略可、最大500文字） |
| `notify` | `bool` | True で Webhook 通知を送信 |
| `mime_type` | `str \| None` | バイト列渡し時の MIME タイプ（デフォルト: `image/jpeg`） |

**戻り値:** `AnalyzeResponse`

#### `list_drafts`

下書き一覧を取得する。必要なスコープ: `ai:analyze`

```python
list_drafts(
    *,
    status: str = "analyzed",
    page: int = 1,
    per_page: int = 50,
) -> DraftListResponse
```

| 引数 | 型 | 説明 |
|------|-----|------|
| `status` | `str` | フィルタ: `"analyzed"` / `"done"` / `"all"`（デフォルト: `"analyzed"`） |
| `page` | `int` | ページ番号（デフォルト: 1） |
| `per_page` | `int` | 1ページあたりの件数（デフォルト: 50, 上限: 100） |

**戻り値:** `DraftListResponse`

#### `get_draft`

下書き詳細を取得する（候補データ含む）。必要なスコープ: `ai:analyze`

```python
get_draft(draft_id: int) -> DraftDetail
```

| 引数 | 型 | 説明 |
|------|-----|------|
| `draft_id` | `int` | 下書き ID |

**戻り値:** `DraftDetail`

#### `delete_draft`

下書きを削除する。必要なスコープ: `ai:analyze`

```python
delete_draft(draft_id: int) -> None
```

| 引数 | 型 | 説明 |
|------|-----|------|
| `draft_id` | `int` | 下書き ID |

#### `close`

内部の HTTP クライアントを閉じる。コンテキストマネージャ使用時は自動的に呼ばれる。

```python
close() -> None
```

---

## データモデル

### JournalLine

仕訳の1明細行を表すデータクラス。

```python
@dataclass
class JournalLine:
    account_id: int
    debit: int = 0
    credit: int = 0
    description: str = ""
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `account_id` | `int` | 勘定科目 ID（必須） |
| `debit` | `int` | 借方金額（デフォルト: 0） |
| `credit` | `int` | 貸方金額（デフォルト: 0） |
| `description` | `str` | 行レベルの摘要（省略可） |

**注意:** 仕訳全体で借方合計と貸方合計が一致する必要があります（複式簿記）。

### JournalCreateResponse

仕訳起票の成功レスポンス。

```python
@dataclass
class JournalCreateResponse:
    id: int
    entry_number: int
```

### JournalDetail

仕訳の詳細情報。`get_journal` / `list_journals` の戻り値で使用。

```python
@dataclass
class JournalDetail:
    id: int
    date: str
    entry_number: int
    description: str
    source: str
    lines: list[JournalLine]
```

### JournalListResponse

仕訳一覧のレスポンス。

```python
@dataclass
class JournalListResponse:
    journals: list[JournalDetail]
    total: int
    page: int
    per_page: int
```

### AnalyzeResponse

AI 解析レスポンス。

```python
@dataclass
class AnalyzeResponse:
    draft_id: int
    suggestions: list[dict]
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `draft_id` | `int` | 作成された下書きの ID |
| `suggestions` | `list[dict]` | 仕訳候補のリスト（各候補に `title`, `date`, `entry_description`, `lines` 等を含む） |

### DraftSummary

下書きのサマリー情報。

```python
@dataclass
class DraftSummary:
    title: str = ""
    date: str = ""
    description: str = ""
    amount: int = 0
    suggestion_count: int = 0
```

### DraftListItem

下書き一覧の1件。`list_drafts` の戻り値で使用。

```python
@dataclass
class DraftListItem:
    id: int
    status: str
    comment: str
    created_at: str
    summary: DraftSummary | None = None
```

### DraftListResponse

下書き一覧のレスポンス。

```python
@dataclass
class DraftListResponse:
    drafts: list[DraftListItem]
    total: int
    page: int
    per_page: int
```

### DraftDetail

下書きの詳細。`get_draft` の戻り値で使用。候補データを含む。

```python
@dataclass
class DraftDetail:
    id: int
    status: str
    comment: str
    created_at: str
    summary: DraftSummary | None = None
    suggestions: list[dict] = field(default_factory=list)
```

---

## 例外クラス

### KakeiboAPIError

API がエラーレスポンスを返した場合の基底例外。

```python
class KakeiboAPIError(Exception):
    status_code: int   # HTTP ステータスコード
    message: str       # サーバーからのエラーメッセージ
```

### AuthenticationError

`KakeiboAPIError` のサブクラス。401 レスポンス時に送出。

```python
class AuthenticationError(KakeiboAPIError):
    # status_code は常に 401
```

**主なエラーメッセージ（サーバーから返される）:**

| ステータス | メッセージ | 原因 |
|-----------|-----------|------|
| 401 | `Authorization ヘッダーが必要です。` | Bearer トークン未指定 |
| 401 | `無効な API キーです。` | キーが無効または無効化済み |
| 403 | `この API キーには ai:analyze 権限がありません。` | スコープ不足 |
| 400 | `date は必須です。` | 日付が未指定 |
| 400 | `description は必須です。` | 摘要が未指定 |
| 400 | `lines は必須です（配列）。` | 明細行が未指定 |
| 400 | `AI API設定が未登録です。` | サーバーでAI設定未完了 |
| 400 | `下書き(id=N)が見つからないか、既に確定済みです。` | 無効な draft_id |
| 404 | `仕訳が見つかりません。` | 指定 ID の仕訳が存在しない |
| 404 | `下書きが見つかりません。` | 指定 ID の下書きが存在しない |
