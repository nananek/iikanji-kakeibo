#!/usr/bin/env python3
"""コンテナ起動スクリプト (alpine 版 / distroless 版 共通)。

``flask db upgrade`` → ``flask seed`` → Gunicorn を起動する
(``FLASK_DEBUG=1`` のときは Flask 開発サーバー)。

distroless イメージにはシェルが無いため、シェルスクリプトではなくこの
スクリプトを両イメージの ENTRYPOINT にしている。コンテナは非 root で動作する
(alpine 版は app uid 100、distroless 版は nonroot uid 65532)。
"""

import os
import subprocess
import sys

DEFAULT_STORAGE_DIR = "/app/data/vouchers"


def run_module(*args: str) -> None:
    subprocess.run([sys.executable, "-m", *args], check=True)


def main() -> None:
    storage_dir = os.environ.get("STORAGE_LOCAL_DIR", DEFAULT_STORAGE_DIR)
    if not os.access(storage_dir, os.W_OK):
        print(
            f"WARNING: {storage_dir} is not writable by uid {os.geteuid()}. "
            "証憑画像の保存に失敗します (ボリュームの所有者を実行ユーザーに"
            "合わせてください)。",
            flush=True,
        )

    print("Running database migrations...", flush=True)
    run_module("flask", "db", "upgrade")

    print("Seeding account types...", flush=True)
    run_module("flask", "seed")

    if os.environ.get("FLASK_DEBUG") == "1":
        print("Starting Flask development server...", flush=True)
        argv = [sys.executable, "-m", "flask", "run", "--host=0.0.0.0"]
    else:
        print("Starting Gunicorn...", flush=True)
        argv = [
            sys.executable,
            "-m",
            "gunicorn",
            "app:create_app()",
            "-c",
            "gunicorn.conf.py",
        ]

    os.execv(sys.executable, argv)


if __name__ == "__main__":
    main()
