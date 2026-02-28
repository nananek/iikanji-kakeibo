"""Gunicorn 設定ファイル

本番環境（Docker コンテナ内）で使用する。
FLASK_DEBUG=1 のときは Flask 開発サーバーが使われる（entrypoint.sh 参照）。
"""

import os

bind = "0.0.0.0:5000"

# シングルユーザーアプリだが、AI処理中に画像配信がブロックされないよう2ワーカー
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))

# AI証憑解析は外部API呼び出しを含むため長め
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

worker_class = "sync"
preload_app = True
