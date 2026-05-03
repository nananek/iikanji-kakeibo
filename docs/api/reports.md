---
layout: default
title: レポート API
---

# レポート API

財務分析用に試算表・損益計算書・月次比較・確定申告集計を JSON で取得できます。すべて **読み取り専用** のため、`read_only=True` の OAuth トークンでも呼び出し可能です。

すべて `reports:read` スコープが必要 (API キー使用時)。OAuth トークンは全スコープ暗黙です。

## 試算表

`GET /api/v1/reports/trial-balance`

期間範囲を指定して全勘定科目の借方・貸方・残高を取得します。

### クエリパラメータ

| パラメータ | デフォルト | 説明 |
|-----------|:---------:|------|
| `year` | 当年 | 対象年度 |
| `period_from` | 0 | 開始期間 (0=期首, 1-12=月, 13-15=決算整理, 16=損益振替) |
| `period_to` | 15 | 終了期間 |

### レスポンス例

```json
{
  "ok": true,
  "year": 2026,
  "period_from": 0,
  "period_to": 15,
  "balances": [
    {
      "account_code": "1010",
      "account_name": "現金",
      "account_type": "asset",
      "normal_balance": "debit",
      "opening": 0,
      "debit": 100000,
      "credit": 30000,
      "balance": 70000
    }
  ]
}
```

## 損益計算書

`GET /api/v1/reports/income-statement`

科目別内訳付きの P/L を取得します。

### クエリパラメータ

| パラメータ | デフォルト | 説明 |
|-----------|:---------:|------|
| `year` | 当年 | 対象年度 |
| `month` | (省略) | 1-12 を指定すると単月、省略時は年間 |

### レスポンス例

```json
{
  "ok": true,
  "year": 2026,
  "month": null,
  "income_total": 4800000,
  "expense_total": 1500000,
  "net_income": 3300000,
  "income_breakdown": [
    {"account_code": "4010", "account_name": "給与収入", "amount": 4800000}
  ],
  "expense_breakdown": [
    {"account_code": "5010", "account_name": "食費", "amount": 600000},
    {"account_code": "5020", "account_name": "住居費", "amount": 900000}
  ]
}
```

## 月次比較

`GET /api/v1/reports/monthly`

年間の収益・費用の月次推移を取得します。

### クエリパラメータ

| パラメータ | デフォルト | 説明 |
|-----------|:---------:|------|
| `year` | 当年 | 対象年度 |

### レスポンス例

```json
{
  "ok": true,
  "year": 2026,
  "expense_accounts": [
    {
      "code": "5010", "name": "食費", "cost_type": "variable",
      "months": [50000, 48000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
      "total": 98000
    }
  ],
  "income_accounts": [...],
  "expense_totals": [50000, 48000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  "income_totals": [400000, 400000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
}
```

## 確定申告集計

`GET /api/v1/reports/tax`

社会保険料・生命保険料・寄附金・iDeCo・医療費控除等の年間集計を取得します。

### クエリパラメータ

| パラメータ | デフォルト | 説明 |
|-----------|:---------:|------|
| `year` | 当年 | 対象年度 |

### レスポンス例

```json
{
  "ok": true,
  "year": 2026,
  "tax_summary": {
    "social_insurance": {
      "label": "社会保険料控除",
      "total": 720000,
      "accounts": [
        {"name": "健康保険料", "amount": 360000},
        {"name": "厚生年金保険料", "amount": 360000}
      ]
    }
  },
  "medical_summary": {
    "total_paid": 80000,
    "total_reimbursed": 30000,
    "net_total": 50000,
    "by_patient": [
      {
        "name": "山田太郎",
        "paid": 80000,
        "reimbursed": 30000,
        "net": 50000,
        "hospitals": [
          {
            "name": "山田クリニック",
            "paid": 80000, "reimbursed": 30000, "net": 50000,
            "provider_type": "clinic"
          }
        ]
      }
    ]
  }
}
```

## エラー

| HTTP | 内容 |
|:-:|------|
| 400 | `month` が範囲外 (1-12 以外) |
| 401 | 認証失敗 |
| 403 | スコープ `reports:read` がない |
