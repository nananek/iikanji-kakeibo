#!/usr/bin/env bash
# .env のシークレット関係を安全なランダム値で初期化するスクリプト
# Usage: ./setup-env.sh
#
# .env.example をベースに .env を生成し、SECRET_KEY と POSTGRES_PASSWORD を
# セキュアなランダム値に置換します。既に .env が存在する場合は上書き確認します。

set -euo pipefail

ENV_FILE=".env"
EXAMPLE_FILE=".env.example"

if [ ! -f "$EXAMPLE_FILE" ]; then
  echo "Error: $EXAMPLE_FILE が見つかりません。server/ ディレクトリで実行してください。" >&2
  exit 1
fi

if [ -f "$ENV_FILE" ]; then
  read -rp ".env は既に存在します。上書きしますか？ [y/N] " answer
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    echo "中止しました。"
    exit 0
  fi
fi

SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

sed \
  -e "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY}|" \
  -e "s|^FLASK_DEBUG=.*|FLASK_DEBUG=0|" \
  -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${DB_PASSWORD}|" \
  -e "s|postgresql://iikanji:iikanji@|postgresql://iikanji:${DB_PASSWORD}@|" \
  "$EXAMPLE_FILE" > "$ENV_FILE"

echo "✅ .env を生成しました:"
echo "   SECRET_KEY     = ${SECRET_KEY:0:12}..."
echo "   POSTGRES_PASSWORD = ${DB_PASSWORD:0:8}..."
echo "   FLASK_DEBUG    = 0"
echo ""
echo "⚠  WEBAUTHN_RP_ID / WEBAUTHN_ORIGIN は環境に合わせて編集してください。"
