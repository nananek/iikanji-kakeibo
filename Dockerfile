# === Build stage ===
FROM python:3.15.0-alpine AS build

WORKDIR /app

RUN apk add --no-cache build-base jpeg-dev zlib-dev

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# === Base runtime (shared) ===
FROM python:3.15.0-alpine AS base

WORKDIR /app

# 実行ユーザー (非 root) を作成する。jpeg/zlib は Pillow のランタイム依存。
RUN apk add --no-cache jpeg zlib \
    && addgroup -S app && adduser -S -G app app

COPY --from=build /install /usr/local
COPY --chown=app:app . .

# 証憑画像の保存先を app ユーザー所有で用意する。named volume は初回
# マウント時にこのディレクトリの所有者を引き継ぐため、非 root でも書ける。
RUN mkdir -p /app/data/vouchers \
    && chown -R app:app /app/data \
    && chown app:app /app

ENV FLASK_APP=app

# === Web ===
FROM base AS web

RUN chmod +x entrypoint.sh
USER app
EXPOSE 5000
CMD ["./entrypoint.sh"]
