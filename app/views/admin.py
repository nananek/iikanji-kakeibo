"""E7 (#114) §16.6: 運用者向け移行進捗ダッシュボード。

`/admin/migration-progress` (HTML) と `/admin/migration-progress.json` (機械処理用)
を提供する。Basic 認証 (環境変数、`require_ops_basic_auth`) で保護し、Flask-Login
のセッションには依存しない (運用者は通常ユーザーではない)。read のみ・破壊操作なし。
"""

from flask import Blueprint, jsonify, render_template

from app.extensions import limiter
from app.services.migration_status import migration_progress_report
from app.services.ops_auth import require_ops_basic_auth

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/migration-progress.json")
@limiter.limit("30/minute")
@require_ops_basic_auth
def migration_progress_json():
    """§16.6 の進捗を JSON で返す (機械処理・cron 監視用)。"""
    return jsonify(migration_progress_report())


@bp.route("/migration-progress")
@limiter.limit("30/minute")
@require_ops_basic_auth
def migration_progress():
    """§16.6 の進捗を Web UI (進捗バー) で表示する。"""
    return render_template(
        "admin/migration_progress.html", report=migration_progress_report()
    )
