from datetime import date

import json

from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.forms.cashbook import CashbookForm
from app.services.fiscal import check_entry_modifiable, get_closed_periods_map, get_restricted_before_year
from app.services.audit import (
    get_effective_user_id, get_allowed_account_codes,
    is_entry_locked_for_owner, is_entry_locked_for_auditor,
    is_acting_as_auditor,
)
from app.views.helpers import get_grouped_accounts

bp = Blueprint("cashbook", __name__, url_prefix="/cashbook")


def _account_name(account_code, user_id):
    if not account_code:
        return None
    a = Account.query.filter_by(user_id=user_id, code=account_code).first()
    return a.name if a else None


@bp.route("/")
@login_required
def index():
    page = request.args.get("page", 1, type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")

    allowed_codes = get_allowed_account_codes()

    query = (
        JournalEntry.query
        .filter_by(user_id=get_effective_user_id(), source="cashbook")
        .order_by(JournalEntry.date.desc(), JournalEntry.entry_number.desc())
    )

    # Lv2: 公開科目を1つも含まない伝票を除外
    if allowed_codes is not None:
        query = query.filter(
            JournalEntry.id.in_(
                db.session.query(JournalEntryLine.journal_entry_id)
                .filter(JournalEntryLine.account_code.in_(allowed_codes))
            )
        )

    if date_from:
        query = query.filter(JournalEntry.date >= date_from)
    if date_to:
        query = query.filter(JournalEntry.date <= date_to)

    entries = query.paginate(page=page, per_page=20, error_out=False)
    return render_template(
        "cashbook/index.html",
        entries=entries,
        date_from=date_from,
        date_to=date_to,
    )


# E3-F PR-B1.1: new() / edit() は GET 専用。フォーム送信は JS が
# entries_builder で暗号化 → POST /api/v1/journals/batch (新規) /
# PUT /api/v1/journals/<id> (更新) に直接送る (E2EE 経路)。
# accounting.create_cashbook_entry / update_cashbook_entry は本 view からは
# 呼ばれなくなったが、accounts.py / medical.py / tests 由来の呼出が残るため
# 関数自体は dual-storage 完了 (PR-D) まで保持する。
@bp.route("/new", methods=["GET"])
@login_required
def new():
    form = CashbookForm()
    user_id = get_effective_user_id()
    allowed_codes = get_allowed_account_codes()
    closed_periods = get_closed_periods_map(user_id)
    restricted_before = get_restricted_before_year(user_id)

    if not form.date.data:
        form.date.data = date.today()

    grouped_accounts = get_grouped_accounts(user_id, allowed_codes)
    payment_account_name = _account_name(form.payment_account_code.data, user_id)
    category_account_name = _account_name(form.category_account_code.data, user_id)
    return render_template(
        "cashbook/form.html", form=form, is_edit=False,
        grouped_accounts=grouped_accounts,
        payment_account_name=payment_account_name,
        category_account_name=category_account_name,
        closed_periods=closed_periods,
        restricted_before_year=restricted_before,
    )


@bp.route("/<int:entry_id>/edit", methods=["GET"])
@login_required
def edit(entry_id):
    user_id = get_effective_user_id()
    allowed_codes = get_allowed_account_codes()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id, source="cashbook"
    ).first_or_404()

    # 伝票ロックチェック
    if not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, entry):
        flash("提出済みの税務科目を含む伝票のため変更できません。", "danger")
        return redirect(url_for("cashbook.index"))
    if is_acting_as_auditor() and allowed_codes is not None and is_entry_locked_for_auditor(entry, allowed_codes):
        flash("事業主勘定を含む伝票のため変更できません。", "danger")
        return redirect(url_for("cashbook.index"))

    # 確定済み期間チェック
    err = check_entry_modifiable(user_id, entry)
    if err:
        flash(err, "danger")
        return redirect(url_for("cashbook.index"))

    form = CashbookForm()
    closed_periods = get_closed_periods_map(user_id)
    restricted_before = get_restricted_before_year(user_id)

    form.date.data = entry.date
    form.description.data = entry.description
    form.fiscal_period.data = str(entry.fiscal_period) if entry.fiscal_period is not None else ""
    # 仕訳明細から元のデータを復元（3方向検出）
    lines = entry.lines
    if len(lines) == 2:
        debit_line = [l for l in lines if l.debit_amount > 0][0]
        credit_line = [l for l in lines if l.credit_amount > 0][0]

        bs_types = {"asset", "liability"}
        debit_account = Account.query.filter_by(user_id=user_id, code=debit_line.account_code).first()
        credit_account = Account.query.filter_by(user_id=user_id, code=credit_line.account_code).first()
        debit_is_bs = debit_account and debit_account.account_type.code in bs_types
        credit_is_bs = credit_account and credit_account.account_type.code in bs_types

        if debit_is_bs and credit_is_bs:
            form.transaction_type.data = "transfer"
            form.payment_account_code.data = credit_line.account_code
            form.category_account_code.data = debit_line.account_code
            form.amount.data = int(debit_line.debit_amount)
        elif debit_is_bs:
            form.transaction_type.data = "income"
            form.payment_account_code.data = debit_line.account_code
            form.category_account_code.data = credit_line.account_code
            form.amount.data = int(debit_line.debit_amount)
        else:
            form.transaction_type.data = "expense"
            form.payment_account_code.data = credit_line.account_code
            form.category_account_code.data = debit_line.account_code
            form.amount.data = int(debit_line.debit_amount)

    grouped_accounts = get_grouped_accounts(user_id, allowed_codes)
    payment_account_name = _account_name(form.payment_account_code.data, user_id)
    category_account_name = _account_name(form.category_account_code.data, user_id)
    return render_template(
        "cashbook/form.html", form=form, is_edit=True, entry=entry,
        grouped_accounts=grouped_accounts,
        payment_account_name=payment_account_name,
        category_account_name=category_account_name,
        closed_periods=closed_periods,
        restricted_before_year=restricted_before,
    )


@bp.route("/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete(entry_id):
    user_id = get_effective_user_id()
    allowed_codes = get_allowed_account_codes()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id, source="cashbook"
    ).first_or_404()

    def _hx_error(msg):
        if request.headers.get("HX-Request"):
            resp = make_response("", 422)
            resp.headers["HX-Reswap"] = "none"
            resp.headers["HX-Trigger"] = json.dumps(
                {"showToast": {"message": msg, "type": "danger"}}
            )
            return resp
        flash(msg, "danger")
        return redirect(url_for("cashbook.index"))

    # 伝票ロックチェック
    if not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, entry):
        return _hx_error("提出済みの税務科目を含む伝票のため削除できません。")
    if is_acting_as_auditor() and allowed_codes is not None and is_entry_locked_for_auditor(entry, allowed_codes):
        return _hx_error("事業主勘定を含む伝票のため削除できません。")

    # 確定済み期間チェック
    err = check_entry_modifiable(user_id, entry)
    if err:
        return _hx_error(err)

    num = entry.entry_number
    db.session.delete(entry)
    db.session.commit()

    if request.headers.get("HX-Request"):
        resp = make_response("", 200)
        resp.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": f"伝票 #{num} を削除しました。", "type": "success"}}
        )
        return resp

    flash(f"伝票 #{num} を削除しました。", "success")
    return redirect(url_for("cashbook.index"))
