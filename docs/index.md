---
layout: default
title: null
---

<div class="hero">
  <h1>いいかんじ™家計簿</h1>
  <p class="tagline">家計簿なのに、ちゃんと複式簿記。</p>
  <div class="hero-links">
    <a href="{{ '/api/' | relative_url }}" class="btn-primary">REST API リファレンス</a>
    <a href="{{ '/client-py/' | relative_url }}" class="btn-outline">Python クライアント</a>
  </div>
</div>

---

出納帳に入力するだけで、裏で仕訳が自動生成。
CSV・OFX・Webページ貼り付けで銀行明細を一括取込。
AIが証憑を読み取って仕訳を提案。
確定申告に必要な控除額も自動集計。

<div class="features">
  <div class="feature">
    <h3>かんたん</h3>
    <p>簿記を知らなくても使える出納帳UI。日付・科目・金額を入力するだけで複式簿記の仕訳伝票を自動生成します。</p>
  </div>
  <div class="feature">
    <h3>ほんかく</h3>
    <p>複式簿記の仕訳帳・元帳・試算表・損益計算書・貸借対照表を完備。月次確定で帳簿の整合性を保護。</p>
  </div>
  <div class="feature">
    <h3>べんり</h3>
    <p>CSV / OFX / Web貼り付け / AI証憑仕訳で入力を省力化。科目自動推定・一括設定・ドラッグ選択も。</p>
  </div>
  <div class="feature">
    <h3>かしこい</h3>
    <p>固定費・変動費・随時費の区分で月末着地を自動予測。確定申告用の控除額も自動集計します。</p>
  </div>
  <div class="feature">
    <h3>つながる</h3>
    <p>3段階の権限レベルで税理士・公認会計士と安全にデータ共有。REST API と Python クライアントで外部連携も。</p>
  </div>
  <div class="feature">
    <h3>まもる</h3>
    <p>Passkey (WebAuthn) 認証・月次確定によるロック・監査用提出ロック。PWA対応でモバイルからも。</p>
  </div>
</div>

## REST API

外部プログラムから仕訳の起票・閲覧・削除、AI 証憑仕訳ができる REST API を提供しています。API キーによる Bearer 認証で、設定画面からキーの発行・スコープ管理が可能です。

- [REST API リファレンス]({{ '/api/' | relative_url }}) — 認証・エンドポイント・エラーコード
- [仕訳 API]({{ '/api/journals.html' | relative_url }}) — 仕訳の CRUD 操作
- [AI 証憑仕訳 API]({{ '/api/ai-drafts.html' | relative_url }}) — 画像解析・下書き管理

## Python クライアント

[`iikanji`](https://github.com/nananek/iikanji-kakeibo-client-py) パッケージで REST API を Python から簡単に呼び出せます。

```python
from iikanji import KakeiboClient, JournalLine

with KakeiboClient("https://your-server.example.com", "ik_your_api_key") as client:
    result = client.create_journal(
        date="2026-02-15",
        description="スーパーで食材購入",
        lines=[
            JournalLine(account_id=12, debit=3000),
            JournalLine(account_id=1, credit=3000),
        ],
    )
```

- [はじめに]({{ '/client-py/' | relative_url }}) — インストールと基本的な使い方
- [API リファレンス]({{ '/client-py/api-reference.html' | relative_url }}) — クラス・メソッド・例外
- [使用例]({{ '/client-py/examples.html' | relative_url }}) — 実践的なサンプルコード

## 活用ガイド

- [iPhone ショートカットで AI 証憑仕訳]({{ '/guides/shortcuts.html' | relative_url }}) — レシートを撮影するだけで仕訳候補を自動作成

## 技術スタック

- Python 3.12 / Flask 3.x / Gunicorn
- PostgreSQL 16 / SQLAlchemy 2.x / Alembic
- Bootstrap 5.3 / Chart.js / PWA
- Docker Compose

## リリースノート

[リリースノート]({{ '/releases.html' | relative_url }}) — 各バージョンの変更履歴

## リンク

- [GitHub リポジトリ (サーバー)](https://github.com/nananek/iikanji-kakeibo)
- [GitHub リポジトリ (Python クライアント)](https://github.com/nananek/iikanji-kakeibo-client-py)
- [いいかんじ™ライセンス (IKL)](https://github.com/nananek/iikanji-kakeibo/blob/master/LICENSE) — MIT 互換
