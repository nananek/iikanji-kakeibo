from datetime import date

import json

from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.forms.cashbook import CashbookForm
from app.services.accounting import (
    create_cashbook_entry,
    create_transfer_entry,
    update_cashbook_entry,
    update_transfer_entry,
)
from app.services.fiscal import check_entry_modifiable, check_period_open_for_new, adjust_date_for_fiscal_period, get_closed_periods_map, get_restricted_before_year
from app.services.audit import (
    get_effective_user_id, get_allowed_account_codes, get_submitted_account_codes,
    is_entry_locked_for_owner, is_entry_locked_for_auditor,
    is_acting_as_auditor,
)
from app.views.helpers import get_grouped_accounts

bp = Blueprint("cashbook", __name__, url_prefix="/cashbook")


def _account_name(account_code, user_id):
    """科目codeから名前を取得"""
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


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    form = CashbookForm()
    user_id = get_effective_user_id()
    allowed_codes = get_allowed_account_codes()
    closed_periods = get_closed_periods_map(user_id)
    restricted_before = get_restricted_before_year(user_id)

    if not form.date.data:
        form.date.data = date.today()

    if form.validate_on_submit():
        # 本人側: ロック科目チェック
        if not is_acting_as_auditor():
            locked_codes = get_submitted_account_codes(user_id)
            if locked_codes:
                used = {form.payment_account_code.data, form.category_account_code.data}
                if used & locked_codes:
                    flash("提出済みの税務科目を含むため登録できません。", "danger")
                    grouped_accounts = get_grouped_accounts(user_id, allowed_codes)
                    payment_account_name = _account_name(form.payment_account_code.data, get_effective_user_id())
                    category_account_name = _account_name(form.category_account_code.data, get_effective_user_id())
                    return render_template(
                        "cashbook/form.html", form=form, is_edit=False,
                        grouped_accounts=grouped_accounts,
                        payment_account_name=payment_account_name,
                        category_account_name=category_account_name,
                        closed_periods=closed_periods,
                        restricted_before_year=restricted_before,
                    )

        # 計上期間の決定
        fiscal_period = None
        if form.fiscal_period.data:
            fiscal_period = int(form.fiscal_period.data)
        # 特殊期間の日付補正
        form.date.data = adjust_date_for_fiscal_period(form.date.data, fiscal_period)
        period = fiscal_period if fiscal_period is not None else form.date.data.month

        # 確定済み期間チェック
        err = check_period_open_for_new(
            user_id, form.date.data.year, period
        )
        if err:
            flash(err, "danger")
            grouped_accounts = get_grouped_accounts(user_id, allowed_codes)
            payment_account_name = _account_name(form.payment_account_code.data, get_effective_user_id())
            category_account_name = _account_name(form.category_account_code.data, get_effective_user_id())
            return render_template(
                "cashbook/form.html", form=form, is_edit=False,
                grouped_accounts=grouped_accounts,
                payment_account_name=payment_account_name,
                category_account_name=category_account_name,
                closed_periods=closed_periods,
                restricted_before_year=restricted_before,
            )

        # 資金移動: 同一科目チェック
        if form.transaction_type.data == "transfer":
            if form.payment_account_code.data == form.category_account_code.data:
                flash("移動元と移動先は異なる科目を選択してください。", "danger")
                grouped_accounts = get_grouped_accounts(user_id, allowed_codes)
                payment_account_name = _account_name(form.payment_account_code.data, get_effective_user_id())
                category_account_name = _account_name(form.category_account_code.data, get_effective_user_id())
                return render_template(
                    "cashbook/form.html", form=form, is_edit=False,
                    grouped_accounts=grouped_accounts,
                    payment_account_name=payment_account_name,
                    category_account_name=category_account_name,
                    closed_periods=closed_periods,
                    restricted_before_year=restricted_before,
                )

        if form.transaction_type.data == "transfer":
            entry = create_transfer_entry(
                user_id=user_id,
                date=form.date.data,
                from_account_code=form.payment_account_code.data,
                to_account_code=form.category_account_code.data,
                amount=form.amount.data,
                description=form.description.data,
                fiscal_period=fiscal_period,
            )
        else:
            entry = create_cashbook_entry(
                user_id=user_id,
                date=form.date.data,
                transaction_type=form.transaction_type.data,
                payment_account_code=form.payment_account_code.data,
                category_account_code=form.category_account_code.data,
                amount=form.amount.data,
                description=form.description.data,
                fiscal_period=fiscal_period,
            )
        flash(f"伝票 #{entry.entry_number} を登録しました。", "success")
        return redirect(url_for("cashbook.index"))

    grouped_accounts = get_grouped_accounts(user_id, allowed_codes)
    payment_account_name = _account_name(form.payment_account_code.data, get_effective_user_id())
    category_account_name = _account_name(form.category_account_code.data, get_effective_user_id())
    return render_template(
        "cashbook/form.html", form=form, is_edit=False,
        grouped_accounts=grouped_accounts,
        payment_account_name=payment_account_name,
        category_account_name=category_account_name,
        closed_periods=closed_periods,
        restricted_before_year=restricted_before,
    )


@bp.route("/<int:entry_id>/edit", methods=["GET", "POST"])
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

    if form.validate_on_submit():
        # 計上期間の決定
        fiscal_period = None
        if form.fiscal_period.data:
            fiscal_period = int(form.fiscal_period.data)
        # 特殊期間の日付補正
        form.date.data = adjust_date_for_fiscal_period(form.date.data, fiscal_period)

        # 変更先の期間が確定済みでないかチェック
        new_period = fiscal_period if fiscal_period is not None else form.date.data.month
        err = check_period_open_for_new(user_id, form.date.data.year, new_period)
        if err:
            flash(err, "danger")
            grouped_accounts = get_grouped_accounts(user_id, allowed_codes)
            payment_account_name = _account_name(form.payment_account_code.data, get_effective_user_id())
            category_account_name = _account_name(form.category_account_code.data, get_effective_user_id())
            return render_template(
                "cashbook/form.html", form=form, is_edit=True, entry=entry,
                grouped_accounts=grouped_accounts,
                payment_account_name=payment_account_name,
                category_account_name=category_account_name,
                closed_periods=closed_periods,
                restricted_before_year=restricted_before,
            )

        # 資金移動: 同一科目チェック
        if form.transaction_type.data == "transfer":
            if form.payment_account_code.data == form.category_account_code.data:
                flash("移動元と移動先は異なる科目を選択してください。", "danger")
                grouped_accounts = get_grouped_accounts(user_id, allowed_codes)
                payment_account_name = _account_name(form.payment_account_code.data, get_effective_user_id())
                category_account_name = _account_name(form.category_account_code.data, get_effective_user_id())
                return render_template(
                    "cashbook/form.html", form=form, is_edit=True, entry=entry,
                    grouped_accounts=grouped_accounts,
                    payment_account_name=payment_account_name,
                    category_account_name=category_account_name,
                    closed_periods=closed_periods,
                    restricted_before_year=restricted_before,
                )

        if form.transaction_type.data == "transfer":
            update_transfer_entry(
                entry=entry,
                date=form.date.data,
                from_account_code=form.payment_account_code.data,
                to_account_code=form.category_account_code.data,
                amount=form.amount.data,
                description=form.description.data,
            )
        else:
            update_cashbook_entry(
                entry=entry,
                date=form.date.data,
                transaction_type=form.transaction_type.data,
                payment_account_code=form.payment_account_code.data,
                category_account_code=form.category_account_code.data,
                amount=form.amount.data,
                description=form.description.data,
            )
        entry.fiscal_period = fiscal_period
        db.session.commit()
        flash(f"伝票 #{entry.entry_number} を更新しました。", "success")
        return redirect(url_for("cashbook.index"))

    if request.method == "GET":
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
                # 資金移動: 移動元=credit側, 移動先=debit側
                form.transaction_type.data = "transfer"
                form.payment_account_code.data = credit_line.account_code
                form.category_account_code.data = debit_line.account_code
                form.amount.data = int(debit_line.debit_amount)
            elif debit_is_bs:
                # 収入: 入金先=debit側, 収入源=credit側
                form.transaction_type.data = "income"
                form.payment_account_code.data = debit_line.account_code
                form.category_account_code.data = credit_line.account_code
                form.amount.data = int(debit_line.debit_amount)
            else:
                # 支出: 支払元=credit側, 支出先=debit側
                form.transaction_type.data = "expense"
                form.payment_account_code.data = credit_line.account_code
                form.category_account_code.data = debit_line.account_code
                form.amount.data = int(debit_line.debit_amount)

    grouped_accounts = get_grouped_accounts(get_effective_user_id(), allowed_codes)
    payment_account_name = _account_name(form.payment_account_code.data, get_effective_user_id())
    category_account_name = _account_name(form.category_account_code.data, get_effective_user_id())
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
