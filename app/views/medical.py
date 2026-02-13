from datetime import date

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.medical import MedicalExpense
from app.forms.medical import MedicalExpenseForm
from app.services.accounting import create_cashbook_entry

bp = Blueprint("medical", __name__, url_prefix="/medical")


def _get_payment_choices(user_id):
    asset_type = AccountType.query.filter_by(code="asset").first()
    liability_type = AccountType.query.filter_by(code="liability").first()
    accounts = (
        Account.query
        .filter(
            Account.user_id == user_id,
            Account.is_active.is_(True),
            Account.account_type_id.in_([asset_type.id, liability_type.id]),
        )
        .order_by(Account.code)
        .all()
    )
    return [(a.id, a.name) for a in accounts]


def _get_medical_account(user_id):
    return Account.query.filter_by(
        user_id=user_id, code="6010"
    ).first()


@bp.route("/")
@login_required
def index():
    page = request.args.get("page", 1, type=int)
    year = request.args.get("year", date.today().year, type=int)

    query = (
        MedicalExpense.query
        .filter(
            MedicalExpense.user_id == current_user.id,
            db.extract("year", MedicalExpense.date) == year,
        )
        .order_by(MedicalExpense.date.desc())
    )

    expenses = query.paginate(page=page, per_page=20, error_out=False)

    # 集計
    all_expenses = (
        MedicalExpense.query
        .filter(
            MedicalExpense.user_id == current_user.id,
            db.extract("year", MedicalExpense.date) == year,
        )
        .all()
    )
    total_paid = sum(e.amount_paid for e in all_expenses)
    total_reimbursed = sum(e.insurance_reimbursement for e in all_expenses)
    net_total = total_paid - total_reimbursed

    return render_template(
        "medical/index.html",
        expenses=expenses,
        year=year,
        total_paid=total_paid,
        total_reimbursed=total_reimbursed,
        net_total=net_total,
        deductible=max(0, net_total - 100000),
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    form = MedicalExpenseForm()
    form.payment_account_id.choices = _get_payment_choices(current_user.id)

    if not form.date.data:
        form.date.data = date.today()

    if form.validate_on_submit():
        medical_account = _get_medical_account(current_user.id)
        if not medical_account:
            flash("医療費の勘定科目が見つかりません。", "danger")
            return render_template("medical/form.html", form=form, is_edit=False)

        # 仕訳を作成
        entry = create_cashbook_entry(
            user_id=current_user.id,
            date=form.date.data,
            transaction_type="expense",
            payment_account_id=form.payment_account_id.data,
            category_account_id=medical_account.id,
            amount=form.amount_paid.data,
            description=f"医療費: {form.hospital_name.data}",
        )

        # 医療費明細を作成
        expense = MedicalExpense(
            user_id=current_user.id,
            journal_entry_id=entry.id,
            date=form.date.data,
            patient_name=form.patient_name.data,
            hospital_name=form.hospital_name.data,
            treatment_description=form.treatment_description.data,
            amount_paid=form.amount_paid.data,
            insurance_reimbursement=form.insurance_reimbursement.data or 0,
        )
        db.session.add(expense)
        db.session.commit()

        flash("医療費を登録しました。", "success")
        return redirect(url_for("medical.index"))

    return render_template("medical/form.html", form=form, is_edit=False)


@bp.route("/<int:expense_id>/edit", methods=["GET", "POST"])
@login_required
def edit(expense_id):
    expense = MedicalExpense.query.filter_by(
        id=expense_id, user_id=current_user.id
    ).first_or_404()

    form = MedicalExpenseForm()
    form.payment_account_id.choices = _get_payment_choices(current_user.id)

    if form.validate_on_submit():
        expense.date = form.date.data
        expense.patient_name = form.patient_name.data
        expense.hospital_name = form.hospital_name.data
        expense.treatment_description = form.treatment_description.data
        expense.amount_paid = form.amount_paid.data
        expense.insurance_reimbursement = form.insurance_reimbursement.data or 0
        db.session.commit()
        flash("医療費を更新しました。", "success")
        return redirect(url_for("medical.index"))

    if request.method == "GET":
        form.date.data = expense.date
        form.patient_name.data = expense.patient_name
        form.hospital_name.data = expense.hospital_name
        form.treatment_description.data = expense.treatment_description
        form.amount_paid.data = expense.amount_paid
        form.insurance_reimbursement.data = expense.insurance_reimbursement

    return render_template("medical/form.html", form=form, is_edit=True, expense=expense)


@bp.route("/<int:expense_id>/delete", methods=["POST"])
@login_required
def delete(expense_id):
    expense = MedicalExpense.query.filter_by(
        id=expense_id, user_id=current_user.id
    ).first_or_404()

    # 紐付いた仕訳も削除
    if expense.journal_entry:
        db.session.delete(expense.journal_entry)

    db.session.delete(expense)
    db.session.commit()
    flash("医療費を削除しました。", "success")
    return redirect(url_for("medical.index"))
