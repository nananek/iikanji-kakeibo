from datetime import date

import json

from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry
from app.forms.cashbook import CashbookForm
from app.services.fiscal import check_entry_modifiable, get_closed_periods_map, get_restricted_before_year
from app.services.audit import (
    get_effective_user_id, get_allowed_account_codes,
    is_entry_locked_for_owner, is_entry_locked_for_auditor,
    is_acting_as_auditor, mask_account_name,
)
from app.views.helpers import get_grouped_accounts

bp = Blueprint("cashbook", __name__, url_prefix="/cashbook")


def _account_name(account_code, user_id):
    if not account_code:
        return None
    a = Account.query.filter_by(user_id=user_id, code=account_code).first()
    return a.name if a else None


def _cashbook_accounts_meta(user_id):
    """出納帳一覧 (cashbook/index.html) のクライアント描画が科目名解決に使う
    code → {name} メタ。account テーブルは非暗号化メタデータなのでサーバ側で
    構築してよい。出納帳仕訳は無効化済み科目も参照しうるため is_active で絞らず
    全科目を含める。監査 Lv2 では allowed_codes でフィルタ + 非公開科目名は
    マスクする (代理閲覧時はクライアント側で復号できず空表示になる)。
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
        a.code: {"name": mask_account_name(a.name, a.code, allowed_codes)}
        for a in accounts
    }


@bp.route("/")
@login_required
def index():
    """出納帳一覧 (E3-F PR-D-4-2 でクライアント描画に移行)。

    クライアントが /api/v1/journals を fiscal_year で取得・MK 復号し、
    source="cashbook" を抽出してテーブルを描画する。サーバ側で平文
    (date / description / line.account 名) は一切読まない。旧 date_from/date_to
    範囲 filter は fiscal_year セレクタに置換 (date は DROP 対象の平文カラム)。
    """
    year = request.args.get("year", date.today().year, type=int)
    user_id = get_effective_user_id()
    return render_template(
        "cashbook/index.html",
        year=year,
        effective_user_id=user_id,
        accounts_meta=_cashbook_accounts_meta(user_id),
    )


# E3-F PR-B1.1: new() / edit() は GET 専用。フォーム送信は JS が
# entries_builder で暗号化 → POST /api/v1/journals/batch (新規) /
# PUT /api/v1/journals/<id> (更新) に直接送る (E2EE 経路)。
# 旧 accounting.create_cashbook_entry / update_cashbook_entry (平文 write) は
# 全 caller 消滅により PR-D-5-4 で削除済。
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

    # E3-F PR-D-6-3b-3: 平文 date / description の prefill 読取を撤去
    # (これらの列は D-6-5 で DROP)。クライアント (edit_form_prefill.js) が
    # encrypted_blob を MK で復号して date / description フィールドを埋める。
    # E3-F PR-D-6-3: fiscal_period prefill は保持列 fiscal_month から行う
    # (両者は書込時に同期されており等価)。
    form.fiscal_period.data = str(entry.fiscal_month) if entry.fiscal_month is not None else ""
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
