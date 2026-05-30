from datetime import date

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required
from app.extensions import db
from app.models.account import Account
from app.models.medical import MedicalExpense
from app.forms.medical import MedicalExpenseForm
from app.services.fiscal import check_entry_modifiable
from app.services.audit import (
    get_effective_user_id, get_allowed_account_codes,
    is_entry_locked_for_owner,
    is_entry_locked_for_auditor, is_acting_as_auditor,
    mask_account_name,
)
from app.views.helpers import get_grouped_accounts

bp = Blueprint("medical", __name__, url_prefix="/medical")


PROVIDER_TYPES = [
    ("", "未設定"),
    ("hospital", "病院"),
    ("pharmacy", "薬局"),
    ("nursing", "介護"),
    ("other", "その他"),
]

PROVIDER_TYPE_LABELS = {k: v for k, v in PROVIDER_TYPES}


def _get_medical_account(user_id):
    return Account.query.filter_by(
        user_id=user_id, code="6010"
    ).first()


def _medical_accounts_meta(user_id):
    """医療費 (tax_category="medical") 科目の code → {name, tax_category} meta。

    クライアント側 (medical/index.html) が仕訳 + MedicalExpense をマージする際に
    医療費科目コードの判定と科目名解決に使う。account テーブルは非暗号化メタ
    データなのでサーバ側で構築してよい。監査 Lv2 では allowed_codes でフィルタ +
    非公開科目名はマスクする (代理閲覧時はクライアント側で復号せず空表示)。
    """
    allowed_codes = get_allowed_account_codes()
    accounts = (
        Account.query.filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    if allowed_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_codes]
    return {
        a.code: {
            "name": mask_account_name(a.name, a.code, allowed_codes),
            "tax_category": a.tax_category,
        }
        for a in accounts
        if a.tax_category == "medical"
    }


@bp.route("/")
@login_required
def index():
    """医療費一覧 (Phase E3-F PR-D-3 でクライアント描画に移行)。

    クライアントが /api/v1/journals と /api/v1/medical-expenses から
    MK 復号してマージ・集計し、テーブル + 合計を描画する。サーバ側で平文
    (date / patient_name / hospital_name 等) は一切読まない。
    """
    year = request.args.get("year", date.today().year, type=int)
    user_id = get_effective_user_id()
    return render_template(
        "medical/index.html",
        year=year,
        effective_user_id=user_id,
        medical_accounts_meta=_medical_accounts_meta(user_id),
    )


@bp.route("/new", methods=["GET"])
@login_required
def new():
    """医療費登録フォーム (GET 専用)。

    保存はクライアント側で仕訳 (batch API) + 医療費明細 (medical-expenses API) を
    暗号化 POST する (medicalNewSubmitE2EE)。サーバ側の平文 POST は撤去済。
    """
    form = MedicalExpenseForm()
    user_id = get_effective_user_id()

    if not form.date.data:
        form.date.data = date.today()

    medical_account = _get_medical_account(user_id)
    grouped_accounts = get_grouped_accounts(user_id, get_allowed_account_codes())
    payment_account_name = None
    if form.payment_account_code.data:
        a = Account.query.filter_by(
            code=form.payment_account_code.data, user_id=user_id,
        ).first()
        payment_account_name = a.name if a else None
    return render_template(
        "medical/form.html", form=form,
        grouped_accounts=grouped_accounts,
        payment_account_name=payment_account_name,
        medical_account_code=medical_account.code if medical_account else None,
        effective_user_id=user_id,
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
        allowed_codes = get_allowed_account_codes()
        if not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, expense.journal_entry):
            flash("提出済みの税務科目を含む伝票のため削除できません。", "danger")
            return redirect(url_for("medical.index"))
        if is_acting_as_auditor() and allowed_codes is not None and is_entry_locked_for_auditor(expense.journal_entry, allowed_codes):
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
