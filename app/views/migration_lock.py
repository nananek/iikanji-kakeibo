"""E7 #114 PR-4b: 鍵未設定ロック (§16.5) の解決ページ。

メンテナンスウィンドウ後の猶予期間 (30 日) を過ぎても MK (暗号鍵) を設定しな
かったユーザーは `migration-lock-stale` (PR-4b-2) により `is_active=False` で
ロックされる。ロック中ユーザーがログインすると `auth.login` が `force=True` で
限定セッションを張り、`migration_lock_gate` (app/__init__.py) が許可エンドポイント
以外をブロックしてこのページへ誘導する。

ここでユーザーは 2 択を取れる:
  1. 鍵を設定して続行 — 既存の鍵設定ウィザード (encryptionKeyWizard) を埋め込む。
     設定が完了すると `users.public_key` が立ち、次リクエストで gate が自己回復
     (`is_active=True` / `locked_at=NULL`) してロックが解ける。
  2. 退会する — `settings.delete_account` フローへ誘導 (§15.5)。
"""

from flask import Blueprint, redirect, render_template, url_for
from flask_login import login_required, current_user

bp = Blueprint("migration_lock", __name__, url_prefix="/migration")


@bp.route("/locked")
@login_required
def locked():
    """鍵未設定ロックの解決ページ。

    ロックされていない (is_active=True) ユーザーが直接来た場合は通常の
    ダッシュボードへ送る (ロック中のみ意味を持つページ)。
    """
    if current_user.is_active:
        return redirect(url_for("dashboard.index"))
    return render_template("migration_lock/locked.html")
