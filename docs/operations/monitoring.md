---
layout: default
title: 監視
---

# 監視

公開 SaaS 運用で検知すべきイベントと、それぞれの監視手段。

## アプリケーションログのキーワード

`logger.warning` / `logger.exception` で記録される運用イベント:

| キーワード | 意味 | 対応 |
|-----------|------|------|
| `voucher deleted: id=...` | 証憑が削除された | 運用フィルタ用の記録 |
| `account_deletion: ...` | ユーザー退会処理中の警告 | 失敗時はストレージ/StorageUsage に drift が残る → `flask storage-audit --fix` |
| `record_upload failed` | 容量加算の DB エラー | quota リーク発生中 → `flask storage-audit --fix` |
| `quota_warning failed` | 容量警告メール送信失敗 | SMTP 設定確認 |
| `_maybe_send_quota_warning failed` | 同上 | 同上 |
| `ai_journal rollback: ...` | TOCTOU 巻き戻し時のストレージ削除失敗 | 孤立ファイルが残る可能性 → 整合性監査 |
| `Failed to send email '...' to ...` | メール送信失敗一般 | SMTP / プロバイダ確認 |

`docker logs server-web-1 | grep -E 'WARNING|ERROR'` で抽出できる。

## ヘルスチェック

`GET /` (ダッシュボード) は認証が必要なため、ヘルスチェック用に
`GET /legal/terms` の HTTP 200 をチェックすることを推奨 (法的文書ページ
は未認証アクセス可能 + 軽量)。

```bash
curl -fs https://your-domain.com/legal/terms -o /dev/null \
  || echo "health check failed"
```

> **Note**: `/legal/terms` は Flask + Jinja2 でテンプレートをレンダー
> するため、コンテナが起動しているだけでなくアプリケーションが正常
> 動作していることを確認できる (= DB 接続障害時にもエラーを検出可能)。
> 一方、テンプレート内で DB 依存描画があると DB 障害時に 500 を返す
> 可能性があり、これは「アプリのみ起動」を確認したい用途では弱点。
> 用途に応じて `/static/...` の HEAD など軽量経路と併用するとよい。

## 容量警告

`quota_warning` メールが SMTP 経由で配信されるかは運用上重要。SMTP
プロバイダ (SES / Mailgun / Resend 等) のダッシュボードで送信成功率を
監視することを推奨。

`User.last_quota_warning_level` で直近の通知レベルを確認できる:
```sql
SELECT email, last_quota_warning_level
FROM users
WHERE last_quota_warning_level IS NOT NULL;
```

## エラー追跡 (Sentry 等)

外部エラー追跡サービスの統合は本フェーズでは未実装。将来的に Sentry SDK
の追加を予定 (`pyproject.toml` / `requirements.txt` への `sentry-sdk[flask]`
追加で対応予定、別 PR)。それまでは Docker ログ + `logger.exception` に
依存する。

## レート制限の調整

Flask-Limiter のデフォルト設定:

| エンドポイント | 制限 | 理由 |
|------------|------|------|
| `auth.login` POST | 10/分 | パスワード総当たり防止 |
| `auth.register` POST | 5/分 | 大量登録防止 |
| `auth.recovery_login` POST | 5/分 | リカバリコード総当たり防止 |
| `legal.contact` POST | 5/時 | お問い合わせ濫用防止 |
| `settings.delete_account` POST | 3/時 | 誤操作防止 |
| `vouchers.attach` POST | 10/分 | アップロード連打防止 |

本番運用で調整が必要な場合は `config.py` の `RATELIMIT_*` 環境変数を
参照。

---

## 推奨される追加監視 (今後)

- 認証失敗率の閾値アラート (ブルートフォース検出)
- 招待トークン発行・消費の Webhook 通知
- StorageUsage drift の自動 fix を Slack/Discord に通知
- 退会ユーザー数の月次集計

これらは Sentry / Grafana / Prometheus などの一般的なツールで対応可能。
本プロジェクトとしての公式統合は今後追加予定。
