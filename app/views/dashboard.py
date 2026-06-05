from datetime import date

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account
from app.models.balance_cache import BalanceCacheBlob
from app.models.journal import JournalEntry

bp = Blueprint("dashboard", __name__)


def _migration_years(user_id):
    """E7 (#114) 再ラップ対象の年度一覧 (je/jel/bcb を走査する fiscal_year)。

    journal_entries.fiscal_year と balance_cache_blobs.year の和集合。医療費は
    年度フィルタなしで全取得、証憑はページ走査するため年度は不要。
    """
    je_years = db.session.query(JournalEntry.fiscal_year).filter_by(
        user_id=user_id,
    ).distinct()
    bcb_years = db.session.query(BalanceCacheBlob.year).filter_by(
        user_id=user_id,
    ).distinct()
    years = {row[0] for row in je_years} | {row[0] for row in bcb_years}
    return sorted(y for y in years if y is not None)


@bp.route("/")
@login_required
def index():
    """ダッシュボード (クライアント描画 / Phase E3-F-4d)。

    月次・年累計サマリと月別推移グラフを `composeDashboardView`
    純粋関数 + `dashboard_renderer.mjs` でクライアント側で算出する。
    サーバはアカウント区分メタだけ渡す。
    """
    today = date.today()
    year = today.year
    month = today.month
    user_id = current_user.id

    accounts = (
        Account.query
        .filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    accounts_meta = {
        a.code: {
            "type": a.account_type.code,
            "name": a.name,
        }
        for a in accounts
    }

    # E7 (#114): temp-MK で暗号化された暫定状態なら再ラップ移行バナーを出す。
    migration_active = current_user.migration_temp_mk is not None
    migration_years = _migration_years(user_id) if migration_active else []

    return render_template(
        "dashboard.html",
        year=year,
        month=month,
        accounts_meta=accounts_meta,
        effective_user_id=user_id,
        migration_active=migration_active,
        migration_years=migration_years,
    )
