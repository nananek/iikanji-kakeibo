# === Build stage ===
FROM python:3.12-alpine AS build

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# === Base runtime (shared) ===
FROM python:3.12-alpine AS base

WORKDIR /app

COPY --from=build /install /usr/local
COPY . .

ENV FLASK_APP=app

# === Web ===
FROM base AS web

RUN chmod +x entrypoint.sh
EXPOSE 5000
CMD ["./entrypoint.sh"]
