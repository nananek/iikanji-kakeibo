from datetime import date

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.forms.cashbook import CashbookForm
from app.services.accounting import (
    create_cashbook_entry,
    update_cashbook_entry,
)
from app.services.fiscal import check_entry_modifiable, check_period_open_for_new, adjust_date_for_fiscal_period, get_closed_periods_map
from app.services.audit import (
    get_effective_user_id, get_allowed_account_ids, get_submitted_account_ids,
    is_entry_locked_for_owner, is_entry_locked_for_auditor,
    is_acting_as_auditor,
)
from app.views.helpers import get_grouped_accounts

bp = Blueprint("cashbook", __name__, url_prefix="/cashbook")


def _account_name(account_id):
    """科目IDから名前を取得"""
    if not account_id:
        return None
    a = Account.query.get(account_id)
    return a.name if a else None


@bp.route("/")
@login_required
def index():
    page = request.args.get("page", 1, type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")

    allowed_ids = get_allowed_account_ids()

    query = (
        JournalEntry.query
        .filter_by(user_id=get_effective_user_id(), source="cashbook")
        .order_by(JournalEntry.date.desc(), JournalEntry.entry_number.desc())
    )

    # Lv2: 公開科目を1つも含まない伝票を除外
    if allowed_ids is not None:
        query = query.filter(
            JournalEntry.id.in_(
                db.session.query(JournalEntryLine.journal_entry_id)
                .filter(JournalEntryLine.account_id.in_(allowed_ids))
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
    allowed_ids = get_allowed_account_ids()
    closed_periods = get_closed_periods_map(user_id)

    if not form.date.data:
        form.date.data = date.today()

    if form.validate_on_submit():
        # 本人側: ロック科目チェック
        if not is_acting_as_auditor():
            locked_ids = get_submitted_account_ids(user_id)
            if locked_ids:
                used = {form.payment_account_id.data, form.category_account_id.data}
                if used & locked_ids:
                    flash("提出済みの税務科目を含むため登録できません。", "danger")
                    grouped_accounts = get_grouped_accounts(user_id, allowed_ids)
                    payment_account_name = _account_name(form.payment_account_id.data)
                    category_account_name = _account_name(form.category_account_id.data)
                    return render_template(
                        "cashbook/form.html", form=form, is_edit=False,
                        grouped_accounts=grouped_accounts,
                        payment_account_name=payment_account_name,
                        category_account_name=category_account_name,
                        closed_periods=closed_periods,
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
            grouped_accounts = get_grouped_accounts(user_id, allowed_ids)
            payment_account_name = _account_name(form.payment_account_id.data)
            category_account_name = _account_name(form.category_account_id.data)
            return render_template(
                "cashbook/form.html", form=form, is_edit=False,
                grouped_accounts=grouped_accounts,
                payment_account_name=payment_account_name,
                category_account_name=category_account_name,
                closed_periods=closed_periods,
            )

        entry = create_cashbook_entry(
            user_id=user_id,
            date=form.date.data,
            transaction_type=form.transaction_type.data,
            payment_account_id=form.payment_account_id.data,
            category_account_id=form.category_account_id.data,
            amount=form.amount.data,
            description=form.description.data,
            fiscal_period=fiscal_period,
        )
        flash(f"伝票 #{entry.entry_number} を登録しました。", "success")
        return redirect(url_for("cashbook.index"))

    grouped_accounts = get_grouped_accounts(user_id, allowed_ids)
    payment_account_name = _account_name(form.payment_account_id.data)
    category_account_name = _account_name(form.category_account_id.data)
    return render_template(
        "cashbook/form.html", form=form, is_edit=False,
        grouped_accounts=grouped_accounts,
        payment_account_name=payment_account_name,
        category_account_name=category_account_name,
        closed_periods=closed_periods,
    )


@bp.route("/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit(entry_id):
    user_id = get_effective_user_id()
    allowed_ids = get_allowed_account_ids()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id, source="cashbook"
    ).first_or_404()

    # 伝票ロックチェック
    if not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, entry):
        flash("提出済みの税務科目を含む伝票のため変更できません。", "danger")
        return redirect(url_for("cashbook.index"))
    if is_acting_as_auditor() and allowed_ids is not None and is_entry_locked_for_auditor(entry, allowed_ids):
        flash("事業主勘定を含む伝票のため変更できません。", "danger")
        return redirect(url_for("cashbook.index"))

    # 確定済み期間チェック
    err = check_entry_modifiable(user_id, entry)
    if err:
        flash(err, "danger")
        return redirect(url_for("cashbook.index"))

    form = CashbookForm()
    closed_periods = get_closed_periods_map(user_id)

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
            grouped_accounts = get_grouped_accounts(user_id, allowed_ids)
            payment_account_name = _account_name(form.payment_account_id.data)
            category_account_name = _account_name(form.category_account_id.data)
            return render_template(
                "cashbook/form.html", form=form, is_edit=True, entry=entry,
                grouped_accounts=grouped_accounts,
                payment_account_name=payment_account_name,
                category_account_name=category_account_name,
                closed_periods=closed_periods,
            )

        update_cashbook_entry(
            entry=entry,
            date=form.date.data,
            transaction_type=form.transaction_type.data,
            payment_account_id=form.payment_account_id.data,
            category_account_id=form.category_account_id.data,
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
        # 仕訳明細から元のデータを復元
        lines = entry.lines
        if len(lines) == 2:
            debit_line = [l for l in lines if l.debit_amount > 0][0]
            credit_line = [l for l in lines if l.credit_amount > 0][0]

            debit_account = Account.query.get(debit_line.account_id)
            if debit_account and debit_account.account_type.code in ("asset", "liability"):
                # 収入パターン: 借方=資産、貸方=収益
                form.transaction_type.data = "income"
                form.payment_account_id.data = debit_line.account_id
                form.category_account_id.data = credit_line.account_id
                form.amount.data = int(debit_line.debit_amount)
            else:
                # 支出パターン: 借方=費用、貸方=資産
                form.transaction_type.data = "expense"
                form.payment_account_id.data = credit_line.account_id
                form.category_account_id.data = debit_line.account_id
                form.amount.data = int(debit_line.debit_amount)

    grouped_accounts = get_grouped_accounts(get_effective_user_id(), allowed_ids)
    payment_account_name = _account_name(form.payment_account_id.data)
    category_account_name = _account_name(form.category_account_id.data)
    return render_template(
        "cashbook/form.html", form=form, is_edit=True, entry=entry,
        grouped_accounts=grouped_accounts,
        payment_account_name=payment_account_name,
        category_account_name=category_account_name,
        closed_periods=closed_periods,
    )


@bp.route("/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete(entry_id):
    user_id = get_effective_user_id()
    allowed_ids = get_allowed_account_ids()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id, source="cashbook"
    ).first_or_404()

    # 伝票ロックチェック
    if not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, entry):
        flash("提出済みの税務科目を含む伝票のため削除できません。", "danger")
        return redirect(url_for("cashbook.index"))
    if is_acting_as_auditor() and allowed_ids is not None and is_entry_locked_for_auditor(entry, allowed_ids):
        flash("事業主勘定を含む伝票のため削除できません。", "danger")
        return redirect(url_for("cashbook.index"))

    # 確定済み期間チェック
    err = check_entry_modifiable(user_id, entry)
    if err:
        flash(err, "danger")
        return redirect(url_for("cashbook.index"))

    num = entry.entry_number
    db.session.delete(entry)
    db.session.commit()
    flash(f"伝票 #{num} を削除しました。", "success")
    return redirect(url_for("cashbook.index"))
