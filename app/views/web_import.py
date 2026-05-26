"""Webページ貼り付け→AI明細取込ビュー

クライアント完結 E2EE モードに統合。POST は AJAX JSON ボディで
parsed_transactions[] + payment_account_code を受け取り、session に保存して
/web-import/confirm への遷移用 URL を返す。LLM 呼出はクライアント側
(web_extract.js) が完了している前提。
"""

import math

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, jsonify
from flask_login import login_required

from app.models.account import Account, AccountType
from app.services.audit import get_effective_user_id
from app.models.ai_config import UserAIConfig
from app.services.fiscal import (
    get_restricted_before_year,
    get_capital_account_code, get_closed_periods_for_dates,
)
from app.views.helpers import (
    get_grouped_accounts, save_import_data, load_import_data, delete_import_data,
)

bp = Blueprint("web_import", __name__, url_prefix="/web-import")

MAX_TEXT_LENGTH = 200_000
MAX_PARSED_ROWS = 1000
MAX_DESC_LENGTH = 500    # 1 行の摘要の上限文字数
MAX_AMOUNT = 10**12      # 1 兆円上限 (現実的に妥当)


def _save_parsed_to_session(parsed_rows, payment_account_code):
    """共通: parsed_rows と payment_account_code を session に保存する。"""
    key = save_import_data(parsed_rows)
    session["web_data_key"] = key
    session["web_payment_account_code"] = payment_account_code
    return key


def _validate_parsed_row(row):
    """parsed_transactions の 1 行を schema validate。失敗時は error message を返す。"""
    if not isinstance(row, dict):
        return "行は dict である必要があります"
    date_val = row.get("date")
    if date_val is not None and not isinstance(date_val, str):
        return "date は文字列または null である必要があります"
    if isinstance(date_val, str) and len(date_val) > 50:
        return "date が長すぎます (max 50)"
    desc = row.get("description")
    if desc is not None and not isinstance(desc, str):
        return "description は文字列である必要があります"
    if isinstance(desc, str) and len(desc) > MAX_DESC_LENGTH:
        return f"description が長すぎます (max {MAX_DESC_LENGTH})"
    for amt_key in ("deposit", "withdrawal"):
        amt = row.get(amt_key)
        if amt is not None and not isinstance(amt, (int, float)):
            return f"{amt_key} は数値である必要があります"
        # NaN / Inf も弾く: NaN は全比較 False なので range check を通過してしまい、
        # 後続の int(nan) が ValueError → 500 を引き起こす (json.loads は NaN を許容)
        if isinstance(amt, (int, float)) and (
            not math.isfinite(amt) or amt < 0 or amt > MAX_AMOUNT
        ):
            return f"{amt_key} が範囲外です (有限の 0〜{MAX_AMOUNT})"
    return None


@bp.route("/", methods=["GET", "POST"])
@login_required
def upload():
    """Step 1: テキスト入力 + 口座選択 → AI解析

    GET: アップロード画面を表示 (E2EE モード対応)
    POST (JSON): クライアント側で抽出済みの parsed_transactions[] を受け取り
                 session に保存する
    """
    user_id = get_effective_user_id()
    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    has_config = config is not None
    config_is_e2ee = bool(config and config.is_e2ee)
    grouped_accounts = get_grouped_accounts(user_id)

    if request.method == "POST":
        # E2EE モード未設定ユーザーが直接 POST しても拒否
        # (UI ではフォームを disabled にしているが、サーバ側でも防御)。
        if not config_is_e2ee:
            return jsonify({
                "error": "E2EE 形式の AI 設定が必要です。"
                "設定画面で API キーを E2EE 形式で登録してください。",
            }), 403

        if not request.is_json:
            return jsonify({
                "error": "JSON ボディが必要です。",
            }), 400

        payload = request.get_json(silent=True) or {}
        parsed = payload.get("parsed_transactions")
        payment_account_code = payload.get("payment_account_code")

        if not isinstance(parsed, list) or not parsed:
            return jsonify({"error": "parsed_transactions が空です。"}), 400
        if len(parsed) > MAX_PARSED_ROWS:
            return jsonify({
                "error": f"行数が多すぎます (上限 {MAX_PARSED_ROWS})。",
            }), 400
        if not payment_account_code:
            return jsonify({
                "error": "payment_account_code が必要です。",
            }), 400

        # 各行を schema validate (description 長さ・金額範囲・型)
        for i, row in enumerate(parsed):
            err = _validate_parsed_row(row)
            if err is not None:
                return jsonify({
                    "error": f"行 {i + 1}: {err}",
                }), 400

        owner_account = Account.query.filter_by(
            user_id=user_id, code=payment_account_code,
        ).first()
        if not owner_account:
            return jsonify({"error": "指定された口座が存在しません。"}), 400

        _save_parsed_to_session(parsed, payment_account_code)
        return jsonify({
            "ok": True,
            "redirect_url": url_for("web_import.confirm"),
        })

    return render_template(
        "web_import/upload.html",
        grouped_accounts=grouped_accounts,
        has_config=has_config,
        config_is_e2ee=config_is_e2ee,
    )


@bp.route("/confirm", methods=["GET"])
@login_required
def confirm():
    """Step 2: 確認画面 (取込は batch API 経由)。

    Phase E3-F-5 で旧サーバ POST 経路を撤去。確定は `submitImportBatch`
    経由のみ。E3-F-1 で acting_as_user_id 解決済のため、監査代理閲覧時も
    batch API でオーナーの仕訳として処理される。
    """
    data_key = session.get("web_data_key")
    payment_account_code = session.get("web_payment_account_code")
    parsed = load_import_data(data_key)
    if not parsed or not payment_account_code:
        flash("データがありません。もう一度入力してください。", "warning")
        return redirect(url_for("web_import.upload"))
    user_id = get_effective_user_id()
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
        "web_import/confirm.html",
        parsed=parsed,
        payment_account=payment_account,
        default_expense_id=default_expense.code if default_expense else 0,
        default_income_id=default_income.code if default_income else 0,
        grouped_accounts=grouped_accounts,
        restricted_before_year=restricted_before,
        closed_periods=closed_periods,
        has_ai_config=has_ai_config,
        capital_code=capital_code,
    )
