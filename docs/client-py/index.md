---
layout: default
title: "Python クライアント: はじめに"
---

# はじめに

[`iikanji`](https://github.com/nananek/iikanji-kakeibo-client-py) は、いいかんじ家計簿サーバーの REST API を Python から呼び出すためのクライアントライブラリです。仕訳の起票・閲覧・削除、AI 証憑仕訳（画像解析・下書き管理）に対応しています。

## インストール

```bash
uv add iikanji
```

または pip:

```bash
pip install iikanji
```

## 前提条件

- Python 3.12 以上
- いいかんじ家計簿サーバーの API キー（`ik_` プレフィックス付き）

API キーはサーバーの **設定 > API キー管理** から発行できます。キー発行時に必要なスコープを選択してください:

| スコープ | 説明 |
|---------|------|
| `journals:create` | 仕訳起票 |
| `journals:read` | 仕訳閲覧 |
| `journals:delete` | 仕訳削除 |
| `ai:analyze` | AI証憑仕訳（解析・下書き管理） |

## 基本的な使い方

```python
from iikanji import KakeiboClient, JournalLine

# コンテキストマネージャで接続を管理
with KakeiboClient("https://your-server.example.com", "ik_your_api_key") as client:
    result = client.create_journal(
        date="2026-02-15",
        description="スーパーで食材購入",
        lines=[
            JournalLine(account_id=12, debit=3000),   # 食費（借方）
            JournalLine(account_id=1, credit=3000),    # 現金（貸方）
        ],
    )
    print(f"仕訳ID: {result.id}, 伝票番号: {result.entry_number}")
```

## コンテキストマネージャを使わない場合

```python
client = KakeiboClient("https://your-server.example.com", "ik_your_api_key")

try:
    result = client.create_journal(
        date="2026-02-15",
        description="電気代",
        lines=[
            JournalLine(account_id=14, debit=8500),    # 水道光熱費
            JournalLine(account_id=2, credit=8500),    # 普通預金
        ],
    )
finally:
    client.close()
```

## エラーハンドリング

```python
from iikanji import KakeiboClient, JournalLine, AuthenticationError, KakeiboAPIError

with KakeiboClient("https://your-server.example.com", "ik_your_api_key") as client:
    try:
        result = client.create_journal(
            date="2026-02-15",
            description="テスト",
            lines=[JournalLine(account_id=12, debit=1000)],
        )
    except AuthenticationError as e:
        print(f"認証エラー: {e.message}")
    except KakeiboAPIError as e:
        print(f"APIエラー [{e.status_code}]: {e.message}")
```
