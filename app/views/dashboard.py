from datetime import date

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from app.services.tax import get_income_expense_summary

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    today = date.today()
    year = today.year
    month = today.month

    monthly = get_income_expense_summary(current_user.id, year, month)
    yearly = get_income_expense_summary(current_user.id, year)

    # 月別推移（1月〜当月）
    monthly_trend = []
    for m in range(1, month + 1):
        s = get_income_expense_summary(current_user.id, year, m)
        monthly_trend.append({
            "month": m,
            "income": int(s["income"]),
            "expense": int(s["expense"]),
        })

    return render_template(
        "dashboard.html",
        year=year,
        month=month,
        monthly=monthly,
        yearly=yearly,
        monthly_trend=monthly_trend,
    )
