# === Build stage ===
FROM python:3.12-slim AS build

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# === Base runtime (shared) ===
FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /install /usr/local
COPY . .

ENV FLASK_APP=app

# === Web ===
FROM base AS web

RUN chmod +x entrypoint.sh
EXPOSE 5000
CMD ["./entrypoint.sh"]

# === Worker (auto-import scheduler) ===
FROM base AS worker

CMD ["sh", "-c", "while true; do sleep ${AUTO_IMPORT_INTERVAL:-1800}; flask auto-import; done"]
