from datetime import date

from flask import Blueprint, render_template, session
from flask_login import login_required

from app.models.account import Account
from app.services.audit import (
    get_effective_user_id, get_allowed_account_codes, mask_account_name,
)

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
    user_id = get_effective_user_id()

    allowed_codes = get_allowed_account_codes()
    accounts = (
        Account.query
        .filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    if allowed_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_codes]

    accounts_meta = {
        a.code: {
            "type": a.account_type.code,
            "name": mask_account_name(a.name, a.code, allowed_codes),
        }
        for a in accounts
    }

    return render_template(
        "dashboard.html",
        year=year,
        month=month,
        accounts_meta=accounts_meta,
        effective_user_id=user_id,
        is_audit_proxy=session.get("acting_as_user_id") is not None,
    )
