---
layout: default
title: MCP サーバー
---

# MCP サーバー

いいかんじ™家計簿は **Model Context Protocol (MCP)** のリモートサーバーを
内蔵しています。Claude のカスタムコネクタや ChatGPT コネクタなど、
MCP クライアントから家計データの分析・仕訳の起票ができます。

## エンドポイント

```
https://your-server.example.com/mcp
```

- トランスポート: **Streamable HTTP** (JSON-RPC 2.0)
- 対応プロトコルバージョン: `2025-06-18` / `2025-03-26` / `2024-11-05`
- 認証: `Authorization: Bearer <token>` (OAuth 2.1 アクセストークン `ikt_*`
  または API キー `ik_*`)

未認証でアクセスすると `401` と `WWW-Authenticate` ヘッダーを返します。
MCP クライアントはこのヘッダーから認可サーバーを自動発見します。

## OAuth 2.1 による接続

MCP クライアントは次の標準フローで接続します。

1. **Protected Resource Metadata** (RFC 9728)
   `GET /.well-known/oauth-protected-resource`
2. **Authorization Server Metadata** (RFC 8414)
   `GET /.well-known/oauth-authorization-server`
3. **動的クライアント登録** (RFC 7591)
   `POST /oauth/register` — `redirect_uris` を登録して `client_id` を取得
4. **認可 (Authorization Code + PKCE)**
   `GET /oauth/authorize` → ユーザーがブラウザでログインし、許可する権限を
   選んで承認 → `redirect_uri` に `code` が返る
5. **トークン取得**
   `POST /oauth/token` (`grant_type=authorization_code`, `code_verifier`)
   → `access_token` (`ikt_*`) と `refresh_token` (`ikr_*`)
6. 以後は `Authorization: Bearer <access_token>` で `/mcp` を呼び出す。
   失効時は `POST /oauth/token` (`grant_type=refresh_token`) で更新、
   `POST /oauth/revoke` で失効できます。

PKCE は `S256` が必須です。

### 権限 (スコープ) の絞り込み

認可画面では、クライアントが要求したスコープを **チェックボックスで
取捨選択** できます。チェックを外した権限は発行トークンに含まれず、
その操作は拒否されます。また、書き込み系スコープを 1 つも選ばなければ
自動的に「読み取り専用」扱いになります。

| スコープ | 内容 |
|---------|------|
| `journals:read` | 仕訳・証憑の閲覧 |
| `journals:create` | 仕訳の起票 |
| `journals:delete` | 仕訳の削除 |
| `ai:analyze` | AI 証憑仕訳（解析・下書き操作） |
| `reports:read` | 試算表・損益・確定申告集計の閲覧 |
| `accounts:read` | 勘定科目の閲覧 |

CLI / TUI などのブラウザレス クライアントは従来どおり
**OAuth 2.0 Device Authorization Grant** (`POST /oauth/device`) も利用できます。
デバイス承認画面でも同様にスコープを絞り込めます。

## 提供ツール

| ツール | 内容 | 必要スコープ |
|--------|------|-------------|
| `list_journals` | 仕訳一覧 | `journals:read` |
| `get_journal` | 仕訳詳細 | `journals:read` |
| `create_journal` | 仕訳起票 | `journals:create` |
| `delete_journal` | 仕訳削除 | `journals:delete` |
| `list_vouchers` | 証憑一覧 | `journals:read` |
| `get_voucher_image` | 証憑画像 (base64) | `journals:read` |
| `list_accounts` | 勘定科目一覧 | `accounts:read` |
| `list_ai_drafts` | AI 下書き一覧 | `ai:analyze` |
| `get_ai_draft` | AI 下書き詳細 | `ai:analyze` |
| `analyze_receipt` | 領収書画像の AI 解析 | `ai:analyze` |
| `delete_ai_draft` | AI 下書き削除 | `ai:analyze` |
| `get_trial_balance` | 試算表 | `reports:read` |
| `get_income_statement` | 損益計算書 | `reports:read` |
| `get_monthly_comparison` | 月次比較 | `reports:read` |
| `get_tax_summary` | 確定申告集計 | `reports:read` |

`tools/list` は、トークンに付与されたスコープで利用可能なツールのみを返します。

## 接続例 (Claude カスタムコネクタ)

1. Claude の「コネクタを追加 → カスタム」で URL に
   `https://your-server.example.com/mcp` を入力
2. ブラウザが開くのでログインし、許可する権限を選んで承認
3. Claude から「今月の支出を教えて」のように話しかけると、
   `get_income_statement` などのツールが呼び出されます

## 生の JSON-RPC 例

```bash
# 初期化
curl -s https://your-server.example.com/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'

# ツール一覧
curl -s https://your-server.example.com/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# 試算表を取得
curl -s https://your-server.example.com/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_trial_balance","arguments":{"year":2026}}}'
```

## セキュリティ

- 認可コードは短命 (60 秒)・単回使用。PKCE (`S256`) 必須。
- リダイレクト URI は登録済みのものと完全一致のみ許可 (オープンリダイレクト防止)。
- アクセストークンは 1 時間、リフレッシュトークンは 30 日で、更新時にローテート。
- トークンはハッシュのみ保存。付与したスコープの範囲でのみ操作可能。
- 発行済みトークンは「設定 &gt; 接続アプリ」からいつでも取り消せます。

> `iikanji-mcp` (stdio 版の別リポジトリ) と本サーバー内蔵の `/mcp` は
> どちらも同じ REST API を利用します。用途に応じて選べます。
