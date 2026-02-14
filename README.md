<div align="center">

# いいかんじ家計簿

### 家計簿なのに、ちゃんと複式簿記。

**出納帳に入力するだけで、裏で仕訳が自動生成。**
**CSV・OFX・Webページ貼り付けで銀行明細を一括取込。**
**AIが証憑を読み取って仕訳を提案。**
**確定申告に必要な控除額も自動集計。**

---

</div>

- **かんたん** — 簿記を知らなくても使える出納帳UI
- **ほんかく** — 複式簿記の仕訳帳・元帳・試算表を完備
- **べんり** — CSV / OFX / Web貼り付け / AI証憑仕訳で入力を省力化
- **かしこい** — 固定費・変動費・随時費の区分で月末着地を自動予測

<div align="center">

---

</div>


## 主な機能

### 出納帳モード

日付・収支の種類・口座・費目・金額・摘要を入力するだけで、裏側で自動的に複式簿記の仕訳伝票を生成します。簿記を知らなくても家計管理ができます。

### 仕訳伝票モード

借方・貸方を自由に入力できる本格的な仕訳入力画面。行の動的追加と貸借バランスのリアルタイムチェック付き。

### 明細取込 (CSV / OFX / Web)

銀行口座やクレジットカードの明細を3つの方法で一括取込:
- **CSV取込** — 列マッピング UI で日付・摘要・入金・出金を指定。Shift-JIS / UTF-8 自動判定
- **OFX取込** — 銀行ダウンロードの OFX/QFX ファイルをそのまま読込
- **Web取込** — Webページのテキストを貼り付けるだけ。AI が明細を自動抽出

### AI 証憑仕訳

領収書・給与明細・請求書の画像をアップロードすると、AI が内容を解析して複数の仕訳案を提案。OpenAI / Google Gemini / Anthropic Claude に対応。

### 月次比較・着地予測

科目別の12ヶ月推移を一覧表示。費用・収入を**固定／変動／随時**に分類し、経過日数から月末の着地予想を自動算出。ドーナツグラフと積み上げ棒グラフで構成比を可視化。

### 医療費管理

受診者・病院名・治療内容・支払額・保険補填額を記録。年間の自己負担額と控除対象額（10 万円超過分）を自動計算し、確定申告の医療費控除に対応します。

### レポート

| レポート | 内容 |
|---|---|
| ダッシュボード | 月次 / 年次の収支サマリーと Chart.js による月別推移グラフ |
| 残高試算表 | 全勘定科目の借方・貸方・残高一覧 |
| 総勘定元帳 | 科目別の取引履歴と残高推移 |
| 収支計算書 | 月次 / 年次の収入・支出を科目別に内訳表示 |
| 月次比較 | 12ヶ月の科目別推移・着地予測・区分分析グラフ |
| 確定申告用集計 | 社会保険料・生命保険料・地震保険料・医療費・寄附金・iDeCo・源泉所得税・住民税を税区分ごとに集計 |

### Passkey (WebAuthn) 認証

パスワードに加え、指紋認証や Face ID などの **Passkey** でログイン可能。設定画面から Passkey の追加・管理・削除ができます。WebAuthn 対応ブラウザで自動的にボタンが表示されます。

### 勘定科目管理

ユーザー登録時に約 40 科目の標準勘定科目を自動投入。確定申告用の税区分（`tax_category`）付き。独自科目の追加・編集も可能。

<details>
<summary>標準勘定科目一覧</summary>

| 区分 | コード | 科目名 | 税区分 |
|---|---|---|---|
| 資産 | 1010 | 現金 | |
| | 1020 | 普通預金 | |
| | 1030 | 定期預金 | |
| | 1040 | 電子マネー | |
| | 1050 | 有価証券 | |
| | 1060 | 未収入金 | |
| 負債 | 2010 | クレジットカード | |
| | 2020 | 未払金 | |
| | 2030 | 借入金 | |
| | 2040 | 住宅ローン | |
| 純資産 | 3010 | 元入金 | |
| | 3020 | 繰越利益 | |
| 収益 | 4010 | 給与収入 | |
| | 4020 | 事業収入 | |
| | 4030 | 利息収入 | |
| | 4040 | 配当収入 | |
| | 4050 | 雑収入 | |
| 費用 | 5010〜5120 | 食費 / 住居費 / 水道光熱費 / 通信費 / 交通費 / 日用品費 / 被服費 / 美容費 / 交際費 / 趣味・娯楽費 / 教育費 / 雑費 | |
| | 6010 | 医療費 | 医療費控除 |
| | 6020〜6070 | 健康保険料 / 厚生年金保険料 / 国民年金保険料 / 国民健康保険料 / 雇用保険料 / 介護保険料 | 社会保険料控除 |
| | 7010〜7020 | 生命保険料 / 個人年金保険料 | 生命保険料控除 |
| | 7030 | 地震保険料 | 地震保険料控除 |
| | 7040〜7050 | ふるさと納税 / その他寄附金 | 寄附金控除 |
| | 7060〜7070 | iDeCo掛金 / 小規模企業共済 | 小規模企業共済等掛金控除 |
| | 8010 | 源泉所得税 | 源泉所得税 |
| | 8020 | 住民税 | 住民税 |

</details>

## 技術スタック

- Python 3.12 / Flask 3.x
- PostgreSQL 16
- SQLAlchemy 2.x / Alembic (Flask-Migrate)
- Flask-Login / Flask-WTF
- py_webauthn (Passkey / WebAuthn)
- Bootstrap 5.3 (CDN)
- Docker Compose

## セットアップ

```bash
# リポジトリをクローン
git clone https://github.com/nananek/iikanji-kakeibo.git
cd iikanji-kakeibo

# 設定ファイルを作成
cp docker-compose.yml.example docker-compose.yml
cp .env.example .env
# 必要に応じて .env の SECRET_KEY やポート番号を変更

# 起動（初回はマイグレーションと勘定科目区分の投入を自動実行）
docker compose up -d
```

ブラウザで http://localhost:5001 を開き、ユーザー登録してください。登録時に標準勘定科目が自動で投入されます。

## プロジェクト構成

```
app/
├── __init__.py          # Flask app factory
├── config.py            # 設定
├── extensions.py        # db, migrate, login_manager
├── models/              # SQLAlchemy モデル
│   ├── user.py          #   User
│   ├── account.py       #   AccountType, Account
│   ├── journal.py       #   JournalEntry, JournalEntryLine
│   ├── medical.py       #   MedicalExpense
│   └── webauthn.py      #   WebAuthnCredential
├── views/               # Blueprint (ルーティング)
│   ├── auth.py          #   認証
│   ├── dashboard.py     #   ダッシュボード
│   ├── cashbook.py      #   出納帳モード
│   ├── journal.py       #   仕訳伝票モード
│   ├── csv_import.py    #   CSV明細取込
│   ├── ofx_import.py    #   OFX明細取込
│   ├── web_import.py    #   Web明細取込 (AI)
│   ├── ai_journal.py    #   AI証憑仕訳
│   ├── medical.py       #   医療費管理
│   ├── reports.py       #   レポート
│   ├── accounts.py      #   勘定科目管理 (JSON API)
│   ├── webauthn.py      #   Passkey WebAuthn API
│   └── settings.py      #   設定 (Passkey・AI API)
├── services/            # ビジネスロジック
│   ├── accounting.py    #   仕訳自動生成
│   ├── csv_import.py    #   CSVパース
│   ├── ofx_import.py    #   OFXパース
│   ├── ai_receipt.py    #   AI証憑解析・Web明細抽出
│   ├── seed.py          #   標準科目の初期データ
│   └── tax.py           #   確定申告集計・月次比較・着地予測
├── forms/               # Flask-WTF フォーム
├── templates/           # Jinja2 テンプレート
└── static/              # CSS / JS
```

## ライセンス

[いいかんじ™ライセンス (IKL) v1.0](./LICENSE) — MIT License 互換
