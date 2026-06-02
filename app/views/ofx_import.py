"""OFX明細取り込みビュー"""

from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_required, current_user

from app.models.account import Account, AccountType
from app.models.ai_config import UserAIConfig
from app.services.ofx_import import parse_ofx
from app.services.fiscal import (
    get_restricted_before_year,
    get_capital_account_code, get_closed_periods_for_dates,
)
from app.views.helpers import (
    get_grouped_accounts, save_import_data, load_import_data, delete_import_data,
)

bp = Blueprint("ofx_import", __name__, url_prefix="/ofx-import")

MAX_OFX_SIZE = 5 * 1024 * 1024  # 5MB


@bp.route("/", methods=["GET", "POST"])
@login_required
def upload():
    """Step 1: OFXファイルアップロード"""
    grouped_accounts = get_grouped_accounts(current_user.id)

    if request.method == "POST":
        ofx_file = request.files.get("ofx_file")
        payment_account_code = request.form.get("payment_account_code")

        if not ofx_file or not ofx_file.filename:
            flash("OFXファイルを選択してください。", "danger")
            return render_template(
                "ofx_import/upload.html",
                grouped_accounts=grouped_accounts,
            )

        if not payment_account_code:
            flash("取込先の口座を選択してください。", "danger")
            return render_template(
                "ofx_import/upload.html",
                grouped_accounts=grouped_accounts,
            )

        file_bytes = ofx_file.read()
        if len(file_bytes) > MAX_OFX_SIZE:
            flash("ファイルサイズが大きすぎます（上限5MB）。", "danger")
            return render_template(
                "ofx_import/upload.html",
                grouped_accounts=grouped_accounts,
            )

        try:
            result = parse_ofx(file_bytes)
        except Exception as e:
            flash(f"OFXファイルの解析に失敗しました: {e}", "danger")
            return render_template(
                "ofx_import/upload.html",
                grouped_accounts=grouped_accounts,
            )

        if not result["rows"]:
            flash("取引データが見つかりませんでした。", "warning")
            return render_template(
                "ofx_import/upload.html",
                grouped_accounts=grouped_accounts,
            )

        # 一時ファイルに保存（Cookieサイズ制限を回避）
        key = save_import_data(result["rows"])
        session["ofx_data_key"] = key
        session["ofx_payment_account_code"] = payment_account_code
        session["ofx_account_info"] = result.get("account_id", "")

        flash(f"{len(result['rows'])}件の取引を検出しました。", "success")
        return redirect(url_for("ofx_import.confirm"))

    return render_template(
        "ofx_import/upload.html",
        grouped_accounts=grouped_accounts,
    )


@bp.route("/confirm", methods=["GET"])
@login_required
def confirm():
    """Step 2: 確認画面 (取込は batch API 経由)。

    Phase E3-F-5 で旧サーバ POST 経路を撤去。確定は `submitImportBatch`
    経由のみ。E3-F-1 で acting_as_user_id 解決済のため、監査代理閲覧時も
    batch API でオーナーの仕訳として処理される。
    """
    data_key = session.get("ofx_data_key")
    payment_account_code = session.get("ofx_payment_account_code")
    parsed = load_import_data(data_key)

    if not parsed or not payment_account_code:
        flash("データがありません。もう一度アップロードしてください。", "warning")
        return redirect(url_for("ofx_import.upload"))

    user_id = current_user.id
    payment_account = Account.query.filter_by(user_id=user_id, code=payment_account_code).first()

    expense_type = AccountType.query.filter_by(code="expense").first()
    default_expense = (
        Account.query
        .filter_by(user_id=user_id, account_type_id=expense_type.id, is_active=True)
        .order_by(Account.code)
        .first()
    )
    revenue_type = AccountType.query.filter_by(code="revenue").first()
    default_income = (
        Account.query
        .filter_by(user_id=user_id, account_type_id=revenue_type.id, is_active=True)
        .order_by(Account.code)
        .first()
    )

    restricted_before = get_restricted_before_year(user_id)
    capital_code = get_capital_account_code(user_id)
    closed_periods = get_closed_periods_for_dates(
        user_id, [r.get("date", "") for r in parsed]
    )
    grouped_accounts = get_grouped_accounts(user_id)
    has_ai_config = UserAIConfig.query.filter_by(user_id=user_id).first() is not None
    return render_template(
        "ofx_import/confirm.html",
        parsed=parsed,
        payment_account=payment_account,
        default_expense_id=default_expense.code if default_expense else 0,
        default_income_id=default_income.code if default_income else 0,
        grouped_accounts=grouped_accounts,
        ofx_account_info=session.get("ofx_account_info", ""),
        restricted_before_year=restricted_before,
        closed_periods=closed_periods,
        has_ai_config=has_ai_config,
        capital_code=capital_code,
    )
