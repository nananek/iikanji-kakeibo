from datetime import date

import json

from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry
from app.forms.cashbook import CashbookForm
from app.services.fiscal import check_entry_modifiable, get_closed_periods_map, get_restricted_before_year
from app.views.helpers import get_grouped_accounts

bp = Blueprint("cashbook", __name__, url_prefix="/cashbook")


def _account_name(account_code, user_id):
    if not account_code:
        return None
    a = Account.query.filter_by(user_id=user_id, code=account_code).first()
    return a.name if a else None


def _cashbook_accounts_meta(user_id):
    """出納帳のクライアント描画 / 編集 prefill が科目解決に使う
    code → {name, type_code} メタ。account テーブルは非暗号化メタデータなので
    サーバ側で構築してよい。出納帳仕訳は無効化済み科目も参照しうるため
    is_active で絞らず全科目を含める (編集 prefill の取引種類判定は無効化済み
    BS 科目も BS と判定する必要があり、旧サーバ実装の全科目検索と等価にする)。

    `type_code` は取引種類 (expense/income/transfer) のクライアント側 3 方向
    検出に使う (BS = asset / liability)。一覧描画は name のみ読むため後方互換。
    """
    accounts = (
        Account.query.filter_by(user_id=user_id)
        .options(joinedload(Account.account_type))
        .order_by(Account.code)
        .all()
    )
    return {
        a.code: {"name": a.name, "type_code": a.account_type.code}
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
    user_id = current_user.id
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
    user_id = current_user.id
    closed_periods = get_closed_periods_map(user_id)
    restricted_before = get_restricted_before_year(user_id)

    if not form.date.data:
        form.date.data = date.today()

    grouped_accounts = get_grouped_accounts(user_id)
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
    user_id = current_user.id
    # E3-F PR-D-6-5: source 列は DROP 済のため source="cashbook" で絞れない。
    # 出納帳一覧はクライアントが復号 blob の source で抽出する (client 描画)。
    # サーバは id + user_id で本人の仕訳のみに限定する。
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id
    ).first_or_404()

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
    # #338 PR2 (方針B): 取引種類・支払/科目・金額の prefill は平文金額・科目コード
    # (debit_amount / credit_amount / account_code) を読む「3 方向検出」をサーバから
    # 撤去し、クライアント (cashbook_prefill.js) が encrypted_blob を MK 復号して
    # 行を取得し同じ検出ロジックで埋める。サーバは accounts_meta (非暗号化の
    # code→name/type_code) のみ供給する。MK ロック中は date/description 同様に
    # 空欄のまま (submit もロック中はブロックされる)。
    grouped_accounts = get_grouped_accounts(user_id)
    return render_template(
        "cashbook/form.html", form=form, is_edit=True, entry=entry,
        grouped_accounts=grouped_accounts,
        accounts_meta=_cashbook_accounts_meta(user_id),
        payment_account_name=None,
        category_account_name=None,
        closed_periods=closed_periods,
        restricted_before_year=restricted_before,
    )


@bp.route("/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete(entry_id):
    user_id = current_user.id
    # E3-F PR-D-6-5: source 列は DROP 済のため source="cashbook" で絞れない。
    # 出納帳一覧はクライアントが復号 blob の source で抽出する (client 描画)。
    # サーバは id + user_id で本人の仕訳のみに限定する。
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id
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
