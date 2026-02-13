from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account, AccountType
from app.forms.account import AccountForm

bp = Blueprint("accounts", __name__, url_prefix="/accounts")


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

    return render_template("accounts/index.html", grouped=grouped)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    form = AccountForm()
    form.account_type_id.choices = [
        (at.id, at.name)
        for at in AccountType.query.order_by(AccountType.display_order).all()
    ]

    if form.validate_on_submit():
        existing = Account.query.filter_by(
            user_id=current_user.id, code=form.code.data
        ).first()
        if existing:
            flash("この科目コードは既に使われています。", "danger")
            return render_template("accounts/form.html", form=form, is_edit=False)

        account = Account(
            user_id=current_user.id,
            account_type_id=form.account_type_id.data,
            code=form.code.data,
            name=form.name.data,
            description=form.description.data or "",
            tax_category=form.tax_category.data or None,
            is_system=False,
            is_active=form.is_active.data,
            display_order=0,
        )
        db.session.add(account)
        db.session.commit()
        flash(f"勘定科目「{account.name}」を追加しました。", "success")
        return redirect(url_for("accounts.index"))

    return render_template("accounts/form.html", form=form, is_edit=False)


@bp.route("/<int:account_id>/edit", methods=["GET", "POST"])
@login_required
def edit(account_id):
    account = Account.query.filter_by(
        id=account_id, user_id=current_user.id
    ).first_or_404()

    form = AccountForm()
    form.account_type_id.choices = [
        (at.id, at.name)
        for at in AccountType.query.order_by(AccountType.display_order).all()
    ]

    if form.validate_on_submit():
        account.name = form.name.data
        account.description = form.description.data or ""
        account.tax_category = form.tax_category.data or None
        account.is_active = form.is_active.data
        if not account.is_system:
            account.code = form.code.data
            account.account_type_id = form.account_type_id.data
        db.session.commit()
        flash(f"勘定科目「{account.name}」を更新しました。", "success")
        return redirect(url_for("accounts.index"))

    if request.method == "GET":
        form.code.data = account.code
        form.name.data = account.name
        form.account_type_id.data = account.account_type_id
        form.description.data = account.description
        form.tax_category.data = account.tax_category or ""
        form.is_active.data = account.is_active

    return render_template(
        "accounts/form.html", form=form, is_edit=True, account=account
    )
