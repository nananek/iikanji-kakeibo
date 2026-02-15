import csv
import io
from datetime import date, timedelta
from decimal import Decimal

from flask import Blueprint, render_template, request, Response
from flask_login import login_required, current_user
from sqlalchemy import func, and_, or_

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.tax import (
    get_tax_summary, get_medical_summary, get_income_expense_summary,
    get_monthly_comparison, get_month_projection,
)
from app.services.audit import get_effective_user_id, get_allowed_account_ids, mask_account_name, is_entry_locked_for_owner
from app.services.fiscal import check_entry_modifiable
from app.views.helpers import get_grouped_accounts

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/")
@login_required
def index():
    return render_template("reports/index.html")


@bp.route("/balance")
@login_required
def balance():
    """残高試算表（期間範囲指定対応）"""
    from app.services.fiscal import PERIOD_LABELS

    year = request.args.get("year", date.today().year, type=int)
    pf = request.args.get("pf", 0, type=int)   # period_from (default: 期首)
    pt = request.args.get("pt", 15, type=int)   # period_to   (default: 決算月3)

    # 範囲の正規化
    pf = max(0, min(16, pf))
    pt = max(pf, min(16, pt))

    account_types = AccountType.query.order_by(AccountType.display_order).all()
    accounts = (
        Account.query
        .filter_by(user_id=get_effective_user_id())
        .order_by(Account.code)
        .all()
    )
    # 有効 OR 無効化年 >= 表示年
    accounts = [a for a in accounts if a.is_active or (a.deactivated_year and a.deactivated_year >= year)]

    # Lv2: 公開科目のみに絞る
    allowed_ids = get_allowed_account_ids()
    if allowed_ids is not None:
        accounts = [a for a in accounts if a.id in allowed_ids]

    start_of_year = date(year, 1, 1)
    pl_type_codes = {"revenue", "expense"}
    bs_type_codes = {"asset", "liability", "equity"}

    def _single_period_condition(p):
        """単一期間のSQLAlchemy条件を返す"""
        if p == 0:
            return JournalEntry.fiscal_period == 0
        elif 1 <= p <= 12:
            m_start = date(year, p, 1)
            m_end = date(year, p + 1, 1) if p < 12 else date(year + 1, 1, 1)
            return or_(
                JournalEntry.fiscal_period == p,
                and_(
                    JournalEntry.fiscal_period.is_(None),
                    JournalEntry.date >= m_start,
                    JournalEntry.date < m_end,
                ),
            )
        else:  # 13-16
            return and_(
                JournalEntry.fiscal_period == p,
                JournalEntry.date >= date(year, 1, 1),
                JournalEntry.date < date(year + 1, 1, 1),
            )

    def _range_filter(p_from, p_to):
        """期間範囲のフィルタ条件リストを返す"""
        conditions = [_single_period_condition(p) for p in range(p_from, p_to + 1)]
        return [or_(*conditions)] if conditions else []

    def _query_sum(account_id, filters, include_closing=False):
        """指定条件での借方・貸方合計を返す"""
        q = (
            db.session.query(
                func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
            )
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .filter(JournalEntryLine.account_id == account_id)
        )
        if not include_closing:
            q = q.filter(JournalEntry.source != "closing")
        for f in filters:
            q = q.filter(f)
        return q.first()

    balances = []
    total_revenue = 0
    total_expense = 0

    incl_closing = pt >= 16
    current_filters = _range_filter(pf, pt)

    for account in accounts:
        is_pl = account.account_type.code in pl_type_codes
        is_bs = account.account_type.code in bs_type_codes
        is_debit_normal = account.account_type.normal_balance == "debit"

        # 当期発生額
        result = _query_sum(account.id, current_filters, include_closing=incl_closing)
        period_debit = int(result[0])
        period_credit = int(result[1])

        # 開始残高
        opening = 0
        if pf > 0:
            prior_incl = pf > 16  # prior range includes 16
            prior_filters = _range_filter(0, pf - 1)
            if is_bs:
                # B/S科目: 当年の範囲開始前 + 前年以前の全累計
                if prior_filters:
                    ob_result = _query_sum(account.id, prior_filters, include_closing=prior_incl)
                    prior_d, prior_c = int(ob_result[0]), int(ob_result[1])
                else:
                    prior_d, prior_c = 0, 0
                before_result = _query_sum(account.id, [JournalEntry.date < start_of_year], include_closing=prior_incl)
                before_d, before_c = int(before_result[0]), int(before_result[1])
                ob_debit = prior_d + before_d
                ob_credit = prior_c + before_c
                if is_debit_normal:
                    opening = ob_debit - ob_credit
                else:
                    opening = ob_credit - ob_debit
            elif is_pl and prior_filters:
                ob_result = _query_sum(account.id, prior_filters, include_closing=prior_incl)
                ob_debit = int(ob_result[0])
                ob_credit = int(ob_result[1])
                if is_debit_normal:
                    opening = ob_debit - ob_credit
                else:
                    opening = ob_credit - ob_debit
        elif is_bs:
            # pf==0 でも前年以前のB/S残高は必要
            before_result = _query_sum(account.id, [JournalEntry.date < start_of_year])
            before_d, before_c = int(before_result[0]), int(before_result[1])
            if is_debit_normal:
                opening = before_d - before_c
            else:
                opening = before_c - before_d

        # 残高
        if is_debit_normal:
            balance_amount = opening + period_debit - period_credit
        else:
            balance_amount = opening + period_credit - period_debit

        # 損益集計（P/L科目）
        if account.account_type.code == "revenue":
            total_revenue += period_credit - period_debit
        elif account.account_type.code == "expense":
            total_expense += period_debit - period_credit

        if period_debit != 0 or period_credit != 0 or opening != 0:
            balances.append({
                "account": account,
                "opening": opening,
                "debit": period_debit,
                "credit": period_credit,
                "balance": balance_amount,
            })

    net_income = total_revenue - total_expense

    return render_template(
        "reports/balance.html",
        year=year,
        pf=pf,
        pt=pt,
        period_labels=PERIOD_LABELS,
        balances=balances,
        account_types=account_types,
        net_income=net_income,
    )


@bp.route("/pl")
@login_required
def pl():
    """収支計算書"""
    year = request.args.get("year", date.today().year, type=int)
    month = request.args.get("month", 0, type=int)

    if month:
        summary = get_income_expense_summary(get_effective_user_id(), year, month)
    else:
        summary = get_income_expense_summary(get_effective_user_id(), year)

    # 科目別内訳
    revenue_type = AccountType.query.filter_by(code="revenue").first()
    expense_type = AccountType.query.filter_by(code="expense").first()

    start = date(year, month or 1, 1)
    if month:
        if month == 12:
            end = date(year + 1, 1, 1)
        else:
            end = date(year, month + 1, 1)
    else:
        end = date(year + 1, 1, 1)

    def get_breakdown(type_id, amount_col):
        return (
            db.session.query(
                Account.name,
                func.coalesce(func.sum(amount_col), 0).label("total"),
            )
            .join(JournalEntryLine, JournalEntryLine.account_id == Account.id)
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .filter(
                Account.user_id == get_effective_user_id(),
                Account.account_type_id == type_id,
                JournalEntry.date >= start,
                JournalEntry.date < end,
            )
            .group_by(Account.name, Account.code)
            .order_by(Account.code)
            .having(func.sum(amount_col) > 0)
            .all()
        )

    income_breakdown = (
        get_breakdown(revenue_type.id, JournalEntryLine.credit_amount)
        if revenue_type else []
    )
    expense_breakdown = (
        get_breakdown(expense_type.id, JournalEntryLine.debit_amount)
        if expense_type else []
    )

    return render_template(
        "reports/pl.html",
        year=year,
        month=month,
        summary=summary,
        income_breakdown=income_breakdown,
        expense_breakdown=expense_breakdown,
    )


@bp.route("/tax")
@login_required
def tax():
    """確定申告用集計"""
    year = request.args.get("year", date.today().year, type=int)

    tax_summary = get_tax_summary(get_effective_user_id(), year)
    medical_summary = get_medical_summary(get_effective_user_id(), year)

    return render_template(
        "reports/tax.html",
        year=year,
        tax_summary=tax_summary,
        medical_summary=medical_summary,
    )


@bp.route("/tax/medical-csv")
@login_required
def medical_csv():
    """医療費集計フォーム Ver 3.1 準拠CSVダウンロード"""
    year = request.args.get("year", date.today().year, type=int)
    medical_summary = get_medical_summary(get_effective_user_id(), year)

    output = io.StringIO()
    writer = csv.writer(output)

    # ヘッダー行（Ver 3.1 準拠: A〜H列）
    writer.writerow([
        "医療を受けた人",
        "病院・薬局などの名称",
        "診療・治療",
        "医薬品購入",
        "介護保険サービス",
        "その他の医療費",
        "支払った医療費の金額",
        "左のうち、補てんされる金額",
    ])

    for e in medical_summary["expenses"]:
        pt = e["provider_type"]
        writer.writerow([
            e["patient_name"],
            e["hospital_name"],
            "該当する" if pt == "hospital" or not pt else "",
            "該当する" if pt == "pharmacy" else "",
            "該当する" if pt == "nursing" else "",
            "該当する" if pt == "other" else "",
            e["amount"],
            e["insurance_reimbursement"] if e["insurance_reimbursement"] else "",
        ])

    csv_data = output.getvalue()
    output.close()

    # BOM付きUTF-8でExcel互換
    bom = "\ufeff"
    return Response(
        bom + csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="iryouhi_{year}.csv"',
        },
    )


@bp.route("/ledger")
@login_required
def ledger():
    """総勘定元帳（期間範囲指定対応）"""
    from app.services.fiscal import PERIOD_LABELS
    from sqlalchemy import case

    year = request.args.get("year", date.today().year, type=int)
    pf = request.args.get("pf", 0, type=int)
    pt = request.args.get("pt", 15, type=int)
    account_id = request.args.get("account_id", 0, type=int)

    pf = max(0, min(16, pf))
    pt = max(pf, min(16, pt))

    allowed_ids = get_allowed_account_ids()

    account_types = AccountType.query.order_by(AccountType.display_order).all()
    accounts = (
        Account.query
        .filter_by(user_id=get_effective_user_id())
        .order_by(Account.code)
        .all()
    )
    # 有効 OR 無効化年 >= 表示年
    accounts = [a for a in accounts if a.is_active or (a.deactivated_year and a.deactivated_year >= year)]

    # Lv2: 公開科目のみ
    if allowed_ids is not None:
        accounts = [a for a in accounts if a.id in allowed_ids]

    # 科目区分ごとにグルーピング
    grouped_accounts = {}
    for at in account_types:
        group = [a for a in accounts if a.account_type_id == at.id]
        if group:
            grouped_accounts[at] = group

    selected_account = None
    entries = []
    carry_forward = 0

    def _single_period_condition(p):
        if p == 0:
            return JournalEntry.fiscal_period == 0
        elif 1 <= p <= 12:
            m_start = date(year, p, 1)
            m_end = date(year, p + 1, 1) if p < 12 else date(year + 1, 1, 1)
            return or_(
                JournalEntry.fiscal_period == p,
                and_(
                    JournalEntry.fiscal_period.is_(None),
                    JournalEntry.date >= m_start,
                    JournalEntry.date < m_end,
                ),
            )
        else:  # 13-16
            return and_(
                JournalEntry.fiscal_period == p,
                JournalEntry.date >= date(year, 1, 1),
                JournalEntry.date < date(year + 1, 1, 1),
            )

    def _range_filter(p_from, p_to):
        conditions = [_single_period_condition(p) for p in range(p_from, p_to + 1)]
        return [or_(*conditions)] if conditions else []

    if account_id:
        # Lv2: 非公開科目へのアクセスをブロック
        if allowed_ids is not None and account_id not in allowed_ids:
            from flask import abort
            abort(403)

        selected_account = Account.query.filter_by(
            id=account_id, user_id=get_effective_user_id()
        ).first()

        if selected_account:
            is_bs = selected_account.account_type.code in {"asset", "liability", "equity"}
            is_debit_normal = selected_account.account_type.normal_balance == "debit"
            start_of_year = date(year, 1, 1)

            # 前期繰越の計算
            carry_forward = 0
            if pf > 0:
                prior_filters = _range_filter(0, pf - 1)
                if is_bs:
                    if prior_filters:
                        cf_result = (
                            db.session.query(
                                func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                                func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
                            )
                            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
                            .filter(JournalEntryLine.account_id == account_id, *prior_filters)
                            .first()
                        )
                        prior_d, prior_c = int(cf_result[0]), int(cf_result[1])
                    else:
                        prior_d, prior_c = 0, 0
                    before_result = (
                        db.session.query(
                            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
                        )
                        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
                        .filter(JournalEntryLine.account_id == account_id, JournalEntry.date < start_of_year)
                        .first()
                    )
                    before_d, before_c = int(before_result[0]), int(before_result[1])
                    total_d, total_c = prior_d + before_d, prior_c + before_c
                    carry_forward = (total_d - total_c) if is_debit_normal else (total_c - total_d)
                else:
                    if prior_filters:
                        cf_result = (
                            db.session.query(
                                func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                                func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
                            )
                            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
                            .filter(JournalEntryLine.account_id == account_id, *prior_filters)
                            .first()
                        )
                        d, c = int(cf_result[0]), int(cf_result[1])
                        carry_forward = (d - c) if is_debit_normal else (c - d)
            elif is_bs:
                before_result = (
                    db.session.query(
                        func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                        func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
                    )
                    .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
                    .filter(JournalEntryLine.account_id == account_id, JournalEntry.date < start_of_year)
                    .first()
                )
                before_d, before_c = int(before_result[0]), int(before_result[1])
                carry_forward = (before_d - before_c) if is_debit_normal else (before_c - before_d)

            # 当期の仕訳明細を取得
            current_filters = _range_filter(pf, pt)

            # effective_period: fiscal_period が NULL なら date の月を使う
            effective_period = case(
                (JournalEntry.fiscal_period.isnot(None), JournalEntry.fiscal_period),
                else_=func.extract("month", JournalEntry.date).cast(db.Integer),
            )

            lines = (
                db.session.query(
                    JournalEntry.date,
                    JournalEntry.entry_number,
                    JournalEntry.description,
                    JournalEntryLine.debit_amount,
                    JournalEntryLine.credit_amount,
                    JournalEntryLine.journal_entry_id,
                    JournalEntry.id.label("entry_id"),
                    JournalEntry.fiscal_period,
                    effective_period.label("effective_period"),
                )
                .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
                .filter(
                    JournalEntryLine.account_id == account_id,
                    *current_filters,
                )
                .order_by(effective_period, JournalEntry.date, JournalEntry.entry_number)
                .all()
            )

            running_balance = carry_forward
            for line in lines:
                debit = int(line.debit_amount)
                credit = int(line.credit_amount)
                if is_debit_normal:
                    running_balance += debit - credit
                else:
                    running_balance += credit - debit

                # 相手科目を取得
                counter_lines = (
                    JournalEntryLine.query
                    .filter(
                        JournalEntryLine.journal_entry_id == line.journal_entry_id,
                        JournalEntryLine.account_id != account_id,
                    )
                    .all()
                )
                counter_names = ", ".join(
                    mask_account_name(a.account.name, a.account_id, allowed_ids)
                    for a in counter_lines
                ) if counter_lines else ""

                entries.append({
                    "date": line.date,
                    "entry_number": line.entry_number,
                    "description": line.description,
                    "counter_account": counter_names,
                    "debit": debit,
                    "credit": credit,
                    "balance": running_balance,
                    "entry_id": line.entry_id,
                    "effective_period": line.effective_period,
                })

    # 各エントリの編集可否を判定
    if entries:
        user_id = get_effective_user_id()
        entry_ids = list({e["entry_id"] for e in entries})
        entry_objs = {
            eo.id: eo
            for eo in JournalEntry.query.filter(JournalEntry.id.in_(entry_ids)).all()
        }
        for e in entries:
            eo = entry_objs.get(e["entry_id"])
            if eo:
                e["is_readonly"] = (
                    check_entry_modifiable(user_id, eo) is not None
                    or is_entry_locked_for_owner(user_id, eo)
                )
            else:
                e["is_readonly"] = True

    # モーダル用: 全科目データ（Lv2なら公開科目のみ）
    all_grouped = get_grouped_accounts(get_effective_user_id(), allowed_ids)

    return render_template(
        "reports/ledger.html",
        year=year,
        pf=pf,
        pt=pt,
        period_labels=PERIOD_LABELS,
        grouped_accounts=grouped_accounts,
        selected_account=selected_account,
        account_id=account_id,
        entries=entries,
        carry_forward=carry_forward,
        all_grouped_accounts=all_grouped,
    )


@bp.route("/monthly")
@login_required
def monthly():
    """月次比較レポート"""
    year = request.args.get("year", date.today().year, type=int)
    comparison = get_monthly_comparison(get_effective_user_id(), year)

    projection = None
    today = date.today()
    if year == today.year and today.day < \
            __import__("calendar").monthrange(year, today.month)[1]:
        projection = get_month_projection(
            get_effective_user_id(), year, today.month, comparison
        )

    return render_template(
        "reports/monthly.html",
        year=year,
        current_month=today.month if year == today.year else None,
        comparison=comparison,
        projection=projection,
    )
