import re
from datetime import date

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.journal import JournalEntry, JournalEntryLine


def _get_account_balance(account):
    """科目の全期間残高を計算"""
    result = db.session.query(
        func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
        func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
    ).filter(
        JournalEntryLine.account_user_id == account.user_id,
        JournalEntryLine.account_code == account.code,
    ).first()
    d, c = int(result[0]), int(result[1])
    if account.account_type.normal_balance == "debit":
        return d - c
    return c - d


def _next_code(code: str, user_id: int) -> str:
    """科目コードの末尾番号をインクリメントして未使用のコードを返す"""
    match = re.match(r"^(.*?)(\d+)$", code)
    if not match:
        return code + "2"
    prefix, num_str = match.group(1), match.group(2)
    width = len(num_str)
    next_num = int(num_str) + 1
    while True:
        candidate = f"{prefix}{next_num:0{width}d}"
        exists = Account.query.filter_by(user_id=user_id, code=candidate).first()
        if not exists:
            return candidate
        next_num += 1

bp = Blueprint("accounts", __name__, url_prefix="/accounts")


TAX_CATEGORIES = [
    ("", "なし"),
    ("social_insurance", "社会保険料控除"),
    ("life_insurance", "生命保険料控除"),
    ("earthquake_insurance", "地震保険料控除"),
    ("medical", "医療費控除"),
    ("donation", "寄附金控除"),
    ("ideco", "小規模企業共済等掛金控除"),
    ("withholding_tax", "源泉所得税"),
    ("resident_tax", "住民税"),
]

COST_TYPES = [
    ("occasional", "随時費"),
    ("fixed", "固定費"),
    ("variable", "変動費"),
]


@bp.route("/")
@login_required
def index():
    account_types = AccountType.query.order_by(AccountType.display_order).all()
    accounts = (
        Account.query
        .filter_by(user_id=current_user.id)
        .order_by(Account.code)
        .all()
    )

    grouped = {}
    for at in account_types:
        grouped[at] = [a for a in accounts if a.account_type_id == at.id]

    expense_type = AccountType.query.filter_by(code="expense").first()
    revenue_type = AccountType.query.filter_by(code="revenue").first()

    tax_category_labels = {k: v for k, v in TAX_CATEGORIES if k}

    # Alpine accountEditor 用: 科目区分ごとの有効科目リスト
    accounts_by_type = {
        at.id: [
            {"code": a.code, "name": a.name}
            for a in grouped[at] if a.is_active
        ]
        for at in account_types
    }

    # 無効化時の残高振替はクライアントで全年度の仕訳を復号して残高を計算する。
    # 対象年度を列挙するため、ユーザーの仕訳が存在する年度の範囲を渡す。
    year_range = db.session.query(
        func.min(JournalEntry.fiscal_year), func.max(JournalEntry.fiscal_year)
    ).filter(JournalEntry.user_id == current_user.id).first()
    if year_range and year_range[0] is not None:
        fiscal_years = list(range(int(year_range[0]), int(year_range[1]) + 1))
    else:
        fiscal_years = []

    return render_template(
        "accounts/index.html",
        grouped=grouped,
        account_types=account_types,
        expense_type_id=expense_type.id if expense_type else 0,
        revenue_type_id=revenue_type.id if revenue_type else 0,
        tax_categories=TAX_CATEGORIES,
        tax_category_labels=tax_category_labels,
        cost_types=COST_TYPES,
        accounts_by_type=accounts_by_type,
        fiscal_years=fiscal_years,
    )


@bp.route("/api/<account_code>")
@login_required
def api_get(account_code):
    """編集・コピー用: アカウントデータをJSONで返す"""
    account = Account.query.filter_by(
        user_id=current_user.id, code=account_code
    ).first_or_404()

    copy = request.args.get("copy") == "1"
    if copy and account.system_role:
        return jsonify({"error": "この科目はコピーできません。"}), 400
    data = {
        "code": account.code,
        "name": account.name,
        "account_type_id": account.account_type_id,
        "description": account.description or "",
        "tax_category": account.tax_category or "",
        "cost_type": account.cost_type or "",
        "is_active": account.is_active,
        "is_system": account.is_system,
        "system_role": account.system_role or "",
    }
    if copy:
        data["code"] = _next_code(account.code, current_user.id)
        data["is_system"] = False
        data["system_role"] = ""

    return jsonify(data)


@bp.route("/api/<account_code>/balance")
@login_required
def api_balance(account_code):
    """科目の残高を返す"""
    account = Account.query.filter_by(
        user_id=current_user.id, code=account_code
    ).first_or_404()
    balance = _get_account_balance(account)
    return jsonify({
        "balance": balance,
        "normal_balance": account.account_type.normal_balance,
        "name": account.name,
    })


@bp.route("/api/new", methods=["POST"])
@login_required
def api_create():
    """新規作成API"""
    data = request.get_json()
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()
    account_type_id = data.get("account_type_id")

    if not code:
        return jsonify({"error": "科目コードは必須です。"}), 400
    if len(code) > 10:
        return jsonify({"error": "科目コードは10文字以内にしてください。"}), 400
    if not name:
        return jsonify({"error": "科目名は必須です。"}), 400
    if len(name) > 100:
        return jsonify({"error": "科目名は100文字以内にしてください。"}), 400
    if not account_type_id:
        return jsonify({"error": "科目区分は必須です。"}), 400

    existing = Account.query.filter_by(
        user_id=current_user.id, code=code
    ).first()
    if existing:
        return jsonify({"error": "この科目コードは既に使われています。"}), 400

    account = Account(
        user_id=current_user.id,
        account_type_id=int(account_type_id),
        code=code,
        name=name,
        description=(data.get("description") or "").strip(),
        tax_category=data.get("tax_category") or None,
        cost_type=data.get("cost_type") or None,
        is_system=False,
        is_active=data.get("is_active", True),
        display_order=0,
    )
    db.session.add(account)
    db.session.commit()

    return jsonify({"success": True, "message": f"勘定科目「{account.name}」を追加しました。"})


@bp.route("/api/<account_code>", methods=["POST"])
@login_required
def api_update(account_code):
    """更新API"""
    account = Account.query.filter_by(
        user_id=current_user.id, code=account_code
    ).first_or_404()

    data = request.get_json()
    name = (data.get("name") or "").strip()

    if not name:
        return jsonify({"error": "科目名は必須です。"}), 400
    if len(name) > 100:
        return jsonify({"error": "科目名は100文字以内にしてください。"}), 400

    account.name = name
    account.description = (data.get("description") or "").strip()
    account.tax_category = data.get("tax_category") or None
    account.cost_type = data.get("cost_type") or None

    new_is_active = data.get("is_active", True)

    # 事業主勘定は無効化不可
    if account.system_role and not new_is_active:
        return jsonify({"error": "この科目はシステムで使用されるため無効化できません。"}), 400

    # 有効→無効 の場合: 残高が残っていれば無効化を拒否する。
    # 残高振替仕訳はクライアントが暗号化して batch API に登録する経路へ移行した
    # ため、ここでは平文の振替仕訳を生成しない。残高がある科目を直接無効化する
    # 事故を防ぐため、残高 != 0 なら 400 を返す安全弁のみ残す (クライアントが
    # 先に振替を登録していれば到達時点で残高は 0 になっている)。
    if account.is_active and not new_is_active:
        balance = _get_account_balance(account)
        if balance != 0:
            return jsonify({"error": "残高がある科目を無効化するには振替先を指定してください。",
                            "needs_transfer": True, "balance": balance}), 400
        account.deactivated_year = date.today().year

    # 無効→有効 の場合: deactivated_year クリア
    if not account.is_active and new_is_active:
        account.deactivated_year = None

    account.is_active = new_is_active

    if not account.is_system:
        code = (data.get("code") or "").strip()
        if not code:
            return jsonify({"error": "科目コードは必須です。"}), 400
        if len(code) > 10:
            return jsonify({"error": "科目コードは10文字以内にしてください。"}), 400

        existing = Account.query.filter_by(
            user_id=current_user.id, code=code
        ).filter(Account.code != account_code).first()
        if existing:
            return jsonify({"error": "この科目コードは既に使われています。"}), 400

        account.code = code
        account_type_id = data.get("account_type_id")
        if account_type_id:
            account.account_type_id = int(account_type_id)

    db.session.commit()

    return jsonify({"success": True, "message": f"勘定科目「{account.name}」を更新しました。"})
