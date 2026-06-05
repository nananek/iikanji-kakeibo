"""運用者向け管理画面 (/admin/*, §16.6) の Basic 認証。

現行の `user_type` (personal/auditor) では admin を表現できないため、暫定で
環境変数 (`OPS_BASIC_AUTH_USER` / `OPS_BASIC_AUTH_PASS`) による Basic 認証を
用いる (設計書 §16.5 選択肢 c)。Flask-HTTPAuth 等の依存は追加せず、標準
ライブラリで実装する。HTTPS 前提・運用者のみアクセスする想定。
"""

import functools
from secrets import compare_digest

from flask import Response, current_app, request


def _unauthorized() -> Response:
    """401 + Basic 認証要求 (ブラウザに認証ダイアログを出させる)。"""
    return Response(
        "認証が必要です。\n",
        401,
        {"WWW-Authenticate": 'Basic realm="ops", charset="UTF-8"'},
    )


def require_ops_basic_auth(view):
    """環境変数の Basic 認証で保護するデコレータ。

    - `OPS_BASIC_AUTH_USER` / `_PASS` のどちらかが未設定なら 503 (機能無効)。
      認証情報なしで管理画面を晒さないためのフェイルクローズ。
    - 認証ヘッダ不在 / 不一致なら 401。ユーザー名・パスワードとも
      `compare_digest` で定数時間比較する。
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        cfg_user = current_app.config.get("OPS_BASIC_AUTH_USER", "")
        cfg_pass = current_app.config.get("OPS_BASIC_AUTH_PASS", "")
        if not cfg_user or not cfg_pass:
            return Response(
                "管理画面は無効です (OPS_BASIC_AUTH_USER / _PASS が未設定)。\n",
                503,
            )
        auth = request.authorization
        if auth is None or auth.type != "basic":
            return _unauthorized()
        # 片方だけ一致でも早期 return しないよう、両方を評価してから AND。
        user_ok = compare_digest((auth.username or ""), cfg_user)
        pass_ok = compare_digest((auth.password or ""), cfg_pass)
        if not (user_ok and pass_ok):
            return _unauthorized()
        return view(*args, **kwargs)

    return wrapper
