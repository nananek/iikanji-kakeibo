from datetime import date

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from app.models.account import Account
from app.services.migration_status import migration_rewrap_years

bp = Blueprint("dashboard", __name__)


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
    migration_years = migration_rewrap_years(user_id) if migration_active else []

    return render_template(
        "dashboard.html",
        year=year,
        month=month,
        accounts_meta=accounts_meta,
        effective_user_id=user_id,
        migration_active=migration_active,
        migration_years=migration_years,
    )
