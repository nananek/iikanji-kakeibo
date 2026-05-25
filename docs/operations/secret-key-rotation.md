---
layout: default
title: SECRET_KEY ローテーション
---

# SECRET_KEY のローテーション

`SECRET_KEY` (`.env` で設定する Flask アプリケーション秘密鍵) のローテーション手順。
v4.x から v5.0 へアップグレードする運用者向けの推奨手順を含む。

## なぜローテーションが必要か

`SECRET_KEY` は本アプリで以下の用途に使われる:

- **Flask セッション cookie の署名** (現在も使用)
- **Flask-Login の remember-me cookie の署名** (現在も使用)
- **CSRF トークンの署名** (現在も使用)
- ~~**`UserAIConfig.api_key_encrypted` の Fernet 暗号化鍵 (`SECRET_KEY` の SHA-256 から導出)**~~ (v5.0 で完全廃止)

v4.x までは外部 AI プロバイダの API キーをサーバ側で Fernet 暗号化して保管しており、
復号鍵は `SECRET_KEY` から導出されていた。`SECRET_KEY` が漏れると過去の DB バックアップから
**全ユーザーの平文 API キーが復元可能**だった。

v5.0 (Phase E2) でこの構造は廃止され、API キーは **クライアント側 MK で AES-256-GCM 暗号化**
されるようになった。サーバには復号鍵が存在しない。`SECRET_KEY` は API キー暗号化用途を失い、
セッション署名等の用途のみが残る。

## いつローテーションすべきか

### 必須

- `SECRET_KEY` が漏えいした疑いがある場合 (リポジトリへの誤コミット・バックアップ流出等)
- v5.0 にアップグレードした **既存** デプロイメント (v4.x で運用していた `SECRET_KEY` が
  過去の DB バックアップに含まれる Fernet 暗号文の復号鍵として残存しているため)

### 任意 (定期実施推奨)

- 1 年に 1 回程度 (一般的なセッション鍵運用)
- 退職者・委託先解除等で `.env` アクセス権を持つ人員に変動があった場合

### v5.0 新規デプロイの場合

- 過去 Fernet 暗号文が存在しないので、初期生成した `SECRET_KEY` をそのまま使い続けて問題なし

## ローテーション手順

### 1. 新 `SECRET_KEY` を生成

```bash
python -c 'import secrets; print(secrets.token_urlsafe(64))'
```

### 2. メンテナンスウィンドウを宣言

ローテーション後、**全ユーザーの既存セッション cookie / remember-me cookie /
CSRF トークンが無効化** され、再ログインが必要になる。事前に告知する。

### 3. `.env` を更新してアプリ再起動

```bash
# .env の SECRET_KEY 行を新値に置換
docker compose up -d --force-recreate web
```

### 4. 旧 `SECRET_KEY` の抹消

- パスワードマネージャ・HashiCorp Vault 等から旧値を削除
- オフライン媒体 (USB 等のバックアップ) からも削除
- 過去の `.env` を git stash / 個人 PC の履歴等に残していないか確認

### 5. (v4.x からのアップグレード時のみ) 過去 Fernet 暗号文の存在確認

v4.x の DB バックアップに `user_ai_configs.api_key_encrypted` カラムの値が
残っている場合、旧 `SECRET_KEY` での復号は理論上可能。バックアップを保持する
必要がない期間 (運用上の最低保存期間を超えた古いバックアップ) は破棄を検討。

```sql
-- v5.0 マイグレ 049 適用前のバックアップに対して
SELECT user_id FROM user_ai_configs WHERE api_key_encrypted IS NOT NULL;
```

該当行があるバックアップに対して旧 `SECRET_KEY` を保持し続けると、漏えい時の
影響範囲が広がる。新 `SECRET_KEY` で過去バックアップは復号できないため、
旧 `SECRET_KEY` 抹消が実効性を持つ。

## 関連

- [バックアップと復旧]({{ '/operations/backup/' | relative_url }}) — `.env` の保管方針
- [v5.0 E2EE 設計書 §11]({{ '/v5-e2ee/#11-e2-設計スケッチ-api-キー-e2ee-化--最小スコープ検証' | relative_url }}) — API キー E2EE 化の設計詳細
