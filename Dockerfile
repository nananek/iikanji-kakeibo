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

# === Web (alpine) ===
FROM base AS web

USER app
EXPOSE 5000
ENTRYPOINT ["python", "/app/entrypoint.py"]

# === 依存の収集 (distroless 版) ===
# distroless イメージは Debian 13 (glibc) の Python 3.13 なので、musl 向けの
# ビルドとは別に glibc 向け wheel を集める。distroless にはパッケージマネージャ
# もシェルも無いため、実行時イメージにはこのステージの成果物だけをコピーする。
FROM python:3.13-slim-trixie AS build-distroless

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && mkdir -p /app/data/vouchers

# === Web (distroless: シェルなし / 非 root uid 65532) ===
# 既定のビルド対象ではない。使うときは --target web-distroless を指定する:
#   docker build --target web-distroless -t iikanji:distroless .
# シェルが無いため起動は entrypoint.py。実行ユーザーは nonroot (uid 65532)。
FROM gcr.io/distroless/python3-debian13:nonroot AS web-distroless

ENV FLASK_APP=app \
    PYTHONPATH=/opt/py/lib/python3.13/site-packages \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=build-distroless /install/lib/python3.13/site-packages /opt/py/lib/python3.13/site-packages
COPY --chown=65532:65532 . .

# 証憑画像の保存先を nonroot (65532) 所有で用意する。named volume は初回
# マウント時にこのディレクトリの所有者を引き継ぐため、非 root でも書ける。
COPY --chown=65532:65532 --from=build-distroless /app/data /app/data

EXPOSE 5000
ENTRYPOINT ["/usr/bin/python3.13", "/app/entrypoint.py"]
