# === Build stage ===
FROM python:3.14-alpine AS build

WORKDIR /app

RUN apk add --no-cache build-base jpeg-dev zlib-dev

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# === Base runtime (shared) ===
FROM python:3.14-alpine AS base

WORKDIR /app

RUN apk add --no-cache jpeg zlib

COPY --from=build /install /usr/local
COPY . .

ENV FLASK_APP=app

# === Web ===
FROM base AS web

RUN chmod +x entrypoint.sh
EXPOSE 5000
CMD ["./entrypoint.sh"]
