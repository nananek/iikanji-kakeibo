from datetime import date

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.journal import JournalEntry
from app.forms.cashbook import CashbookForm
from app.services.accounting import (
    create_cashbook_entry,
    update_cashbook_entry,
)

bp = Blueprint("cashbook", __name__, url_prefix="/cashbook")


def _get_account_choices(user_id):
    """出納帳用の科目選択肢を生成"""
    asset_type = AccountType.query.filter_by(code="asset").first()
    liability_type = AccountType.query.filter_by(code="liability").first()
    revenue_type = AccountType.query.filter_by(code="revenue").first()
    expense_type = AccountType.query.filter_by(code="expense").first()

    payment_accounts = (
        Account.query
        .filter(
            Account.user_id == user_id,
            Account.is_active.is_(True),
            Account.account_type_id.in_([asset_type.id, liability_type.id]),
        )
        .order_by(Account.code)
        .all()
    )

    expense_accounts = (
        Account.query
        .filter(
            Account.user_id == user_id,
            Account.is_active.is_(True),
            Account.account_type_id == expense_type.id,
        )
        .order_by(Account.code)
        .all()
    )

    revenue_accounts = (
        Account.query
        .filter(
            Account.user_id == user_id,
            Account.is_active.is_(True),
            Account.account_type_id == revenue_type.id,
        )
        .order_by(Account.code)
        .all()
    )

    payment_choices = [(a.id, f"{a.name}") for a in payment_accounts]
    # 費用+収益を合わせた費目選択肢
    category_choices = (
        [(a.id, f"【支出】{a.name}") for a in expense_accounts]
        + [(a.id, f"【収入】{a.name}") for a in revenue_accounts]
    )

    return payment_choices, category_choices


@bp.route("/")
@login_required
def index():
    page = request.args.get("page", 1, type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")

    query = (
        JournalEntry.query
        .filter_by(user_id=current_user.id, source="cashbook")
        .order_by(JournalEntry.date.desc(), JournalEntry.entry_number.desc())
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
    payment_choices, category_choices = _get_account_choices(current_user.id)
    form.payment_account_id.choices = payment_choices
    form.category_account_id.choices = category_choices

    if not form.date.data:
        form.date.data = date.today()

    if form.validate_on_submit():
        entry = create_cashbook_entry(
            user_id=current_user.id,
            date=form.date.data,
            transaction_type=form.transaction_type.data,
            payment_account_id=form.payment_account_id.data,
            category_account_id=form.category_account_id.data,
            amount=form.amount.data,
            description=form.description.data,
        )
        flash(f"伝票 #{entry.entry_number} を登録しました。", "success")
        return redirect(url_for("cashbook.index"))

    return render_template("cashbook/form.html", form=form, is_edit=False)


@bp.route("/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit(entry_id):
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=current_user.id, source="cashbook"
    ).first_or_404()

    form = CashbookForm()
    payment_choices, category_choices = _get_account_choices(current_user.id)
    form.payment_account_id.choices = payment_choices
    form.category_account_id.choices = category_choices

    if form.validate_on_submit():
        update_cashbook_entry(
            entry=entry,
            date=form.date.data,
            transaction_type=form.transaction_type.data,
            payment_account_id=form.payment_account_id.data,
            category_account_id=form.category_account_id.data,
            amount=form.amount.data,
            description=form.description.data,
        )
        flash(f"伝票 #{entry.entry_number} を更新しました。", "success")
        return redirect(url_for("cashbook.index"))

    if request.method == "GET":
        form.date.data = entry.date
        form.description.data = entry.description
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

    return render_template("cashbook/form.html", form=form, is_edit=True, entry=entry)


@bp.route("/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete(entry_id):
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=current_user.id, source="cashbook"
    ).first_or_404()

    num = entry.entry_number
    db.session.delete(entry)
    db.session.commit()
    flash(f"伝票 #{num} を削除しました。", "success")
    return redirect(url_for("cashbook.index"))
