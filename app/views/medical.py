from datetime import date

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.medical import MedicalExpense
from app.forms.medical import MedicalExpenseForm
from app.services.accounting import create_cashbook_entry
from app.services.fiscal import check_entry_modifiable, check_period_open_for_new
from app.services.audit import (
    get_effective_user_id, get_allowed_account_ids,
    get_submitted_account_ids, is_entry_locked_for_owner,
    is_entry_locked_for_auditor, is_acting_as_auditor,
)
from app.views.helpers import get_grouped_accounts

bp = Blueprint("medical", __name__, url_prefix="/medical")


def _get_medical_account(user_id):
    return Account.query.filter_by(
        user_id=user_id, code="6010"
    ).first()


@bp.route("/")
@login_required
def index():
    year = request.args.get("year", date.today().year, type=int)
    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)

    # 医療費科目（tax_category="medical"）への仕訳明細を取得
    rows = (
        db.session.query(
            JournalEntry.id.label("entry_id"),
            JournalEntry.date,
            JournalEntry.description,
            JournalEntryLine.debit_amount,
            Account.name.label("account_name"),
        )
        .join(JournalEntryLine, JournalEntryLine.journal_entry_id == JournalEntry.id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .filter(
            JournalEntry.user_id == get_effective_user_id(),
            Account.tax_category == "medical",
            JournalEntryLine.debit_amount > 0,
            JournalEntry.date >= start,
            JournalEntry.date < end,
        )
        .order_by(JournalEntry.date.desc())
        .all()
    )

    # 紐付く MedicalExpense レコードを取得（詳細情報用）
    entry_ids = [r.entry_id for r in rows]
    medical_details = {}
    if entry_ids:
        me_records = (
            MedicalExpense.query
            .filter(MedicalExpense.journal_entry_id.in_(entry_ids))
            .all()
        )
        for me in me_records:
            medical_details[me.journal_entry_id] = me

    # テンプレート用のデータ構築
    expenses = []
    for r in rows:
        me = medical_details.get(r.entry_id)
        expenses.append({
            "entry_id": r.entry_id,
            "date": r.date,
            "description": r.description,
            "amount": int(r.debit_amount),
            "account_name": r.account_name,
            "patient_name": me.patient_name if me else "",
            "hospital_name": me.hospital_name if me else "",
            "treatment_description": me.treatment_description if me else "",
            "provider_type": me.provider_type if me else "",
            "insurance_reimbursement": me.insurance_reimbursement if me else 0,
            "medical_expense_id": me.id if me else None,
        })

    total_paid = sum(e["amount"] for e in expenses)
    total_reimbursed = sum(e["insurance_reimbursement"] for e in expenses)
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

    if not form.date.data:
        form.date.data = date.today()

    if form.validate_on_submit():
        user_id = get_effective_user_id()

        # 本人側: ロック科目チェック（医療費科目 + 支払科目）
        if not is_acting_as_auditor():
            locked_ids = get_submitted_account_ids(user_id)
            if locked_ids:
                medical_account = _get_medical_account(user_id)
                used = {form.payment_account_id.data}
                if medical_account:
                    used.add(medical_account.id)
                if used & locked_ids:
                    flash("提出済みの税務科目を含むため登録できません。", "danger")
                    allowed_ids = get_allowed_account_ids()
                    grouped_accounts = get_grouped_accounts(user_id, allowed_ids)
                    return render_template(
                        "medical/form.html", form=form, is_edit=False,
                        grouped_accounts=grouped_accounts,
                    )

        # 確定済み期間チェック
        err = check_period_open_for_new(
            user_id, form.date.data.year, form.date.data.month
        )
        if err:
            flash(err, "danger")
            grouped_accounts = get_grouped_accounts(user_id)
            return render_template(
                "medical/form.html", form=form, is_edit=False,
                grouped_accounts=grouped_accounts,
            )

        medical_account = _get_medical_account(user_id)
        if not medical_account:
            flash("医療費の勘定科目が見つかりません。", "danger")
            grouped_accounts = get_grouped_accounts(get_effective_user_id())
            return render_template(
                "medical/form.html", form=form, is_edit=False,
                grouped_accounts=grouped_accounts,
            )

        entry = create_cashbook_entry(
            user_id=get_effective_user_id(),
            date=form.date.data,
            transaction_type="expense",
            payment_account_id=form.payment_account_id.data,
            category_account_id=medical_account.id,
            amount=form.amount_paid.data,
            description=f"医療費: {form.hospital_name.data}",
        )

        expense = MedicalExpense(
            user_id=get_effective_user_id(),
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

    grouped_accounts = get_grouped_accounts(get_effective_user_id())
    payment_account_name = None
    if form.payment_account_id.data:
        a = Account.query.get(form.payment_account_id.data)
        payment_account_name = a.name if a else None
    return render_template(
        "medical/form.html", form=form, is_edit=False,
        grouped_accounts=grouped_accounts,
        payment_account_name=payment_account_name,
    )


@bp.route("/<int:expense_id>/edit", methods=["GET", "POST"])
@login_required
def edit(expense_id):
    expense = MedicalExpense.query.filter_by(
        id=expense_id, user_id=get_effective_user_id()
    ).first_or_404()

    form = MedicalExpenseForm()

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

    grouped_accounts = get_grouped_accounts(get_effective_user_id())
    payment_account_name = None
    if form.payment_account_id.data:
        a = Account.query.get(form.payment_account_id.data)
        payment_account_name = a.name if a else None
    return render_template(
        "medical/form.html", form=form, is_edit=True, expense=expense,
        grouped_accounts=grouped_accounts,
        payment_account_name=payment_account_name,
    )


@bp.route("/<int:expense_id>/delete", methods=["POST"])
@login_required
def delete(expense_id):
    user_id = get_effective_user_id()
    expense = MedicalExpense.query.filter_by(
        id=expense_id, user_id=user_id
    ).first_or_404()

    # 伝票ロックチェック
    if expense.journal_entry:
        allowed_ids = get_allowed_account_ids()
        if not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, expense.journal_entry):
            flash("提出済みの税務科目を含む伝票のため削除できません。", "danger")
            return redirect(url_for("medical.index"))
        if is_acting_as_auditor() and allowed_ids is not None and is_entry_locked_for_auditor(expense.journal_entry, allowed_ids):
            flash("事業主勘定を含む伝票のため削除できません。", "danger")
            return redirect(url_for("medical.index"))

    # 確定済み期間チェック
    if expense.journal_entry:
        err = check_entry_modifiable(get_effective_user_id(), expense.journal_entry)
        if err:
            flash(err, "danger")
            return redirect(url_for("medical.index"))

    # 紐付いた仕訳も削除
    if expense.journal_entry:
        db.session.delete(expense.journal_entry)

    db.session.delete(expense)
    db.session.commit()
    flash("医療費を削除しました。", "success")
    return redirect(url_for("medical.index"))


PROVIDER_TYPES = [
    ("", "未設定"),
    ("hospital", "病院"),
    ("pharmacy", "薬局"),
    ("nursing", "介護"),
    ("other", "その他"),
]

PROVIDER_TYPE_LABELS = {k: v for k, v in PROVIDER_TYPES}


@bp.route("/api/<int:entry_id>")
@login_required
def api_get(entry_id):
    """仕訳IDから医療費詳細をJSON返却"""
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=get_effective_user_id()
    ).first_or_404()

    me = MedicalExpense.query.filter_by(journal_entry_id=entry_id).first()

    return jsonify({
        "entry_id": entry_id,
        "patient_name": me.patient_name if me else "",
        "hospital_name": me.hospital_name if me else "",
        "provider_type": me.provider_type or "" if me else "",
        "treatment_description": me.treatment_description if me else "",
        "insurance_reimbursement": me.insurance_reimbursement if me else 0,
    })


@bp.route("/api/<int:entry_id>", methods=["POST"])
@login_required
def api_update(entry_id):
    """医療費詳細を作成 or 更新"""
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=get_effective_user_id()
    ).first_or_404()

    data = request.get_json()

    me = MedicalExpense.query.filter_by(journal_entry_id=entry_id).first()

    # 仕訳の医療費科目への借方合計を amount_paid として使用
    medical_lines = (
        db.session.query(JournalEntryLine.debit_amount)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .filter(
            JournalEntryLine.journal_entry_id == entry_id,
            Account.tax_category == "medical",
            JournalEntryLine.debit_amount > 0,
        )
        .all()
    )
    amount_paid = sum(int(line.debit_amount) for line in medical_lines)

    if me:
        me.patient_name = (data.get("patient_name") or "").strip()
        me.hospital_name = (data.get("hospital_name") or "").strip()
        me.provider_type = data.get("provider_type") or None
        me.treatment_description = (data.get("treatment_description") or "").strip()
        me.insurance_reimbursement = int(data.get("insurance_reimbursement") or 0)
        me.amount_paid = amount_paid
    else:
        me = MedicalExpense(
            user_id=get_effective_user_id(),
            journal_entry_id=entry_id,
            date=entry.date,
            patient_name=(data.get("patient_name") or "").strip(),
            hospital_name=(data.get("hospital_name") or "").strip(),
            provider_type=data.get("provider_type") or None,
            treatment_description=(data.get("treatment_description") or "").strip(),
            amount_paid=amount_paid,
            insurance_reimbursement=int(data.get("insurance_reimbursement") or 0),
        )
        db.session.add(me)

    db.session.commit()
    return jsonify({"success": True})


@bp.route("/api/suggestions")
@login_required
def api_suggestions():
    """受診者名・病院名の補完候補を返す"""
    patients = (
        db.session.query(MedicalExpense.patient_name)
        .filter(
            MedicalExpense.user_id == get_effective_user_id(),
            MedicalExpense.patient_name != "",
        )
        .distinct()
        .order_by(MedicalExpense.patient_name)
        .all()
    )
    hospitals = (
        db.session.query(MedicalExpense.hospital_name)
        .filter(
            MedicalExpense.user_id == get_effective_user_id(),
            MedicalExpense.hospital_name != "",
        )
        .distinct()
        .order_by(MedicalExpense.hospital_name)
        .all()
    )
    return jsonify({
        "patients": [r[0] for r in patients],
        "hospitals": [r[0] for r in hospitals],
    })
