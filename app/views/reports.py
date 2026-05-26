import csv
import io
from datetime import date, timedelta
from decimal import Decimal

from flask import Blueprint, render_template, request, Response
from flask_login import login_required, current_user
from sqlalchemy import func

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.tax import (
    get_tax_summary, get_medical_summary, get_income_expense_summary,
    get_monthly_comparison, get_month_projection,
)
from flask_login import current_user as _current_user
from app.services.audit import get_effective_user_id, get_allowed_account_codes, mask_account_name, is_entry_locked_for_owner
from app.services.fiscal import check_entry_modifiable, period_range_filter, get_closed_period
from app.services.balance_cache import get_cached_balances
from app.views.helpers import get_grouped_accounts

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/")
@login_required
def index():
    return render_template("reports/index.html")


@bp.route("/balance")
@login_required
def balance():
    """残高試算表 (クライアント描画)。サーバ集計は撤去済 (Phase E3-F-3a)。
    クライアントが /api/v1/journals から自分の MK で復号して集計する。
    """
    from app.services.fiscal import PERIOD_LABELS

    year = request.args.get("year", date.today().year, type=int)
    if "pf" not in request.args and "pt" not in request.args:
        pref = _current_user.get_pref("reports_default_period", "all")
        if pref == "current_month":
            pf = pt = date.today().month
        else:
            pf, pt = 0, 15
    else:
        pf = request.args.get("pf", 0, type=int)
        pt = request.args.get("pt", 15, type=int)
    pf = max(0, min(16, pf))
    pt = max(pf, min(16, pt))

    user_id = get_effective_user_id()
    accounts = (
        Account.query
        .filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    accounts = [a for a in accounts if a.is_active or (a.deactivated_year and a.deactivated_year >= year)]
    allowed_codes = get_allowed_account_codes()
    if allowed_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_codes]

    # クライアント描画用に accountsMeta を JSON で渡す。
    # `accounts` は line 62-63 で allowed_codes フィルタ適用済みのため
    # Lv2 で非公開の科目はここに含まれない (= 監査者に名前が漏れない)。
    # mask_account_name は防御的多層化のための残置 (allowed_codes フィルタを
    # 将来うっかり外しても「事業主」マスクは効く)。
    accounts_meta = {
        a.code: {
            "type": a.account_type.code,
            "normal_balance": a.account_type.normal_balance,
            "name": mask_account_name(a.name, a.code, allowed_codes),
        }
        for a in accounts
    }

    return render_template(
        "reports/balance.html",
        year=year,
        pf=pf,
        pt=pt,
        period_labels=PERIOD_LABELS,
        accounts_meta=accounts_meta,
        effective_user_id=user_id,
    )


@bp.route("/bs")
@login_required
def bs():
    """貸借対照表 (クライアント描画)。サーバ集計は撤去済 (Phase E3-F-3c)。
    クライアントが min_year..year の全 entries を MK で復号して累計集計。
    """
    year = request.args.get("year", date.today().year, type=int)
    user_id = get_effective_user_id()

    allowed_codes = get_allowed_account_codes()
    all_accounts = (
        Account.query
        .filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    all_accounts = [
        a for a in all_accounts
        if a.is_active or (a.deactivated_year and a.deactivated_year >= year)
    ]
    if allowed_codes is not None:
        all_accounts = [a for a in all_accounts if a.code in allowed_codes]

    # B/S 累計の最古年度。仕訳ゼロなら None で fetch ループを skip
    min_year = (
        db.session.query(func.min(JournalEntry.fiscal_year))
        .filter(JournalEntry.user_id == user_id)
        .scalar()
    )

    # accounts_meta: name は allowed_codes フィルタ適用済のみ含む
    # (Lv2 非公開はここに含まれない)。type/normal_balance はクライアント
    # 側 computeBalanceSheet が必要とする情報。
    accounts_meta = {
        a.code: {
            "type": a.account_type.code,
            "normal_balance": a.account_type.normal_balance,
            "name": mask_account_name(a.name, a.code, allowed_codes),
        }
        for a in all_accounts
    }

    return render_template(
        "reports/bs.html",
        year=year,
        accounts_meta=accounts_meta,
        min_year=min_year,
        effective_user_id=user_id,
    )


@bp.route("/pl")
@login_required
def pl():
    """損益計算書 (クライアント描画)。サーバ集計は撤去済 (Phase E3-F-3b)。
    クライアントが /api/v1/journals から自分の MK で復号して集計する。
    事業所得 (biz_income) は現時点ではサーバ計算結果を JSON で渡す
    (BCB / tax_form クライアント完結化は後続 PR)。
    """
    from app.services.tax_form import get_business_account_codes, get_business_income

    year = request.args.get("year", date.today().year, type=int)
    month = request.args.get("month", 0, type=int)
    user_id = get_effective_user_id()

    accounts = (
        Account.query
        .filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    allowed_codes = get_allowed_account_codes()
    if allowed_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_codes]

    # 事業科目セット (TaxFormMapping 経由) — P/L はこれらを除外して集計する
    biz_codes = get_business_account_codes(user_id)

    # accounts_meta: name は allowed_codes フィルタ適用済みのみ (Lv2 非公開は除外)。
    # is_business は P/L 側で除外用フラグ。
    accounts_meta = {
        a.code: {
            "type": a.account_type.code,
            "name": mask_account_name(a.name, a.code, allowed_codes),
            "is_business": a.code in biz_codes,
        }
        for a in accounts
    }

    biz_income = get_business_income(user_id, year, month or None)

    return render_template(
        "reports/pl.html",
        year=year,
        month=month,
        accounts_meta=accounts_meta,
        biz_income=biz_income,
        effective_user_id=user_id,
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
    if "pf" not in request.args and "pt" not in request.args:
        pref = _current_user.get_pref("reports_default_period", "all")
        if pref == "current_month":
            pf = pt = date.today().month
        else:
            pf, pt = 0, 15
    else:
        pf = request.args.get("pf", 0, type=int)
        pt = request.args.get("pt", 15, type=int)
    account_code = request.args.get("account_code", "")
    sort_order = request.args.get("sort", _current_user.get_pref("ledger_sort_order", "asc"))
    if sort_order not in ("asc", "desc"):
        sort_order = "asc"

    pf = max(0, min(16, pf))
    pt = max(pf, min(16, pt))

    user_id = get_effective_user_id()
    allowed_codes = get_allowed_account_codes()

    account_types = AccountType.query.order_by(AccountType.display_order).all()
    accounts = (
        Account.query
        .filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    # 有効 OR 無効化年 >= 表示年
    accounts = [a for a in accounts if a.is_active or (a.deactivated_year and a.deactivated_year >= year)]

    # Lv2: 公開科目のみ
    if allowed_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_codes]

    # 科目区分ごとにグルーピング
    grouped_accounts = {}
    for at in account_types:
        group = [a for a in accounts if a.account_type_id == at.id]
        if group:
            grouped_accounts[at] = group

    selected_account = None
    entries = []
    carry_forward = 0

    def _query_sum_ledger(acct_code, filter_cond, include_closing=False):
        """元帳用: 借方・貸方合計を返す"""
        q = (
            db.session.query(
                func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
            )
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .filter(
                JournalEntryLine.account_user_id == user_id,
                JournalEntryLine.account_code == acct_code,
            )
        )
        if not include_closing:
            q = q.filter(JournalEntry.source != "closing")
        if filter_cond is not None:
            q = q.filter(filter_cond)
        return q.first()

    if account_code:
        # Lv2: 非公開科目へのアクセスをブロック
        if allowed_codes is not None and account_code not in allowed_codes:
            from flask import abort
            abort(403)

        selected_account = Account.query.filter_by(
            user_id=user_id, code=account_code,
        ).first()

        if selected_account:
            is_bs = selected_account.account_type.code in {"asset", "liability", "equity"}
            is_debit_normal = selected_account.account_type.normal_balance == "debit"
            start_of_year = date(year, 1, 1)

            # 前期繰越の計算（キャッシュ活用）
            carry_forward = 0
            closed = get_closed_period(user_id, year)
            use_cache = pf > 0 and closed >= pf - 1
            cache = get_cached_balances(user_id, year, pf - 1) if use_cache else {}

            if pf > 0 and use_cache and account_code in cache:
                year_d, year_c = cache[account_code]
                if is_bs:
                    before_result = _query_sum_ledger(account_code, JournalEntry.date < start_of_year, include_closing=True)
                    before_d, before_c = int(before_result[0]), int(before_result[1])
                    carry_forward = (year_d + before_d - year_c - before_c) if is_debit_normal else (year_c + before_c - year_d - before_d)
                else:
                    carry_forward = (year_d - year_c) if is_debit_normal else (year_c - year_d)
            elif pf > 0:
                # フォールバック: 従来の全計算
                prior_filter = period_range_filter(year, 0, pf - 1)
                if is_bs:
                    if prior_filter is not None:
                        cf_result = _query_sum_ledger(account_code, prior_filter)
                        prior_d, prior_c = int(cf_result[0]), int(cf_result[1])
                    else:
                        prior_d, prior_c = 0, 0
                    before_result = _query_sum_ledger(account_code, JournalEntry.date < start_of_year, include_closing=True)
                    before_d, before_c = int(before_result[0]), int(before_result[1])
                    total_d, total_c = prior_d + before_d, prior_c + before_c
                    carry_forward = (total_d - total_c) if is_debit_normal else (total_c - total_d)
                else:
                    if prior_filter is not None:
                        cf_result = _query_sum_ledger(account_code, prior_filter)
                        d, c = int(cf_result[0]), int(cf_result[1])
                        carry_forward = (d - c) if is_debit_normal else (c - d)
            elif is_bs:
                before_result = _query_sum_ledger(account_code, JournalEntry.date < start_of_year, include_closing=True)
                before_d, before_c = int(before_result[0]), int(before_result[1])
                carry_forward = (before_d - before_c) if is_debit_normal else (before_c - before_d)

            # 当期の仕訳明細を取得
            current_filter = period_range_filter(year, pf, pt)

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
                    JournalEntryLine.account_user_id == user_id,
                    JournalEntryLine.account_code == account_code,
                    current_filter,
                )
                .order_by(
                    effective_period.desc() if sort_order == "desc" else effective_period,
                    JournalEntry.date.desc() if sort_order == "desc" else JournalEntry.date,
                    JournalEntry.entry_number.desc() if sort_order == "desc" else JournalEntry.entry_number,
                )
                .all()
            )

            if sort_order == "desc":
                # desc: 期末残高から逆算
                total_delta = sum(
                    (int(l.debit_amount) - int(l.credit_amount)) if is_debit_normal
                    else (int(l.credit_amount) - int(l.debit_amount))
                    for l in lines
                )
                running_balance = carry_forward + total_delta
            else:
                running_balance = carry_forward

            for line in lines:
                debit = int(line.debit_amount)
                credit = int(line.credit_amount)
                delta = (debit - credit) if is_debit_normal else (credit - debit)
                if sort_order == "desc":
                    # desc: この行の残高を表示してから引く
                    pass  # running_balance is already the balance after this line
                else:
                    running_balance += delta

                # 相手科目を取得
                counter_lines = (
                    JournalEntryLine.query
                    .filter(
                        JournalEntryLine.journal_entry_id == line.journal_entry_id,
                        JournalEntryLine.account_code != account_code,
                    )
                    .all()
                )
                counter_names = ", ".join(
                    mask_account_name(a.account.name, a.account_code, allowed_codes)
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
                if sort_order == "desc":
                    running_balance -= delta

    # 各エントリの編集可否・証憑を判定
    if entries:
        entry_ids = list({e["entry_id"] for e in entries})
        entry_objs = {
            eo.id: eo
            for eo in JournalEntry.query.filter(JournalEntry.id.in_(entry_ids)).all()
        }
        from app.models.voucher import Voucher
        voucher_map = {}
        # 論理削除済は除外 (一覧表示や is_readonly 判定で「証憑あり」と
        # 誤判定されないように)
        voucher_rows = Voucher.active().filter(
            Voucher.journal_entry_id.in_(entry_ids)
        ).all()
        for v in voucher_rows:
            voucher_map.setdefault(v.journal_entry_id, []).append(v)
        for e in entries:
            eo = entry_objs.get(e["entry_id"])
            if eo:
                e["is_readonly"] = (
                    check_entry_modifiable(user_id, eo) is not None
                    or is_entry_locked_for_owner(user_id, eo)
                )
            else:
                e["is_readonly"] = True
            vlist = voucher_map.get(e["entry_id"])
            e["voucher_id"] = vlist[0].id if vlist else None

    # モーダル用: 全科目データ（Lv2なら公開科目のみ）
    all_grouped = get_grouped_accounts(user_id, allowed_codes)

    return render_template(
        "reports/ledger.html",
        year=year,
        pf=pf,
        pt=pt,
        sort=sort_order,
        period_labels=PERIOD_LABELS,
        grouped_accounts=grouped_accounts,
        selected_account=selected_account,
        account_code=account_code,
        entries=entries,
        carry_forward=carry_forward,
        all_grouped_accounts=all_grouped,
    )


@bp.route("/tax-form")
@login_required
def tax_form_report():
    """青色申告決算書レポート"""
    from app.services.tax_form import get_tax_form_report

    year = request.args.get("year", date.today().year, type=int)
    form_type = request.args.get("form_type", "general")
    if form_type not in ("general", "real_estate"):
        form_type = "general"
    user_id = get_effective_user_id()

    field_data = get_tax_form_report(user_id, year, form_type=form_type)

    section_labels = {
        "revenue": "売上（収入）",
        "cost_of_sales": "売上原価",
        "expenses": "経費",
        "income": "所得金額",
        "bs_assets": "資産の部",
        "bs_liabilities": "負債・資本の部",
    }

    return render_template(
        "reports/tax_form.html",
        year=year,
        form_type=form_type,
        field_data=field_data,
        section_labels=section_labels,
    )


@bp.route("/monthly")
@login_required
def monthly():
    """月次比較レポート"""
    from app.services.tax_form import get_business_account_codes

    year = request.args.get("year", date.today().year, type=int)
    user_id = get_effective_user_id()
    comparison = get_monthly_comparison(user_id, year)

    # 事業科目を折りたたみ
    biz_codes = get_business_account_codes(user_id)
    biz_monthly = None
    if biz_codes:
        biz_monthly = _collapse_business_accounts(comparison, biz_codes)

    projection = None
    today = date.today()
    if year == today.year and today.day <= \
            __import__("calendar").monthrange(year, today.month)[1]:
        method = current_user.get_pref("projection_method", "pro_rata")
        projection = get_month_projection(
            user_id, year, today.month, comparison,
            method=method,
        )

    return render_template(
        "reports/monthly.html",
        year=year,
        current_month=today.month if year == today.year else None,
        comparison=comparison,
        projection=projection,
        biz_monthly=biz_monthly,
    )


def _collapse_business_accounts(comparison, biz_codes):
    """事業科目を comparison から除外し、事業所得の月次データを返す"""
    biz_revenue_months = [0] * 12
    biz_expense_months = [0] * 12

    # 事業科目を抽出して除外
    household_income = []
    household_expense = []

    for a in comparison["income_accounts"]:
        if a["code"] in biz_codes:
            for i in range(12):
                biz_revenue_months[i] += a["months"][i]
        else:
            household_income.append(a)

    for a in comparison["expense_accounts"]:
        if a["code"] in biz_codes:
            for i in range(12):
                biz_expense_months[i] += a["months"][i]
        else:
            household_expense.append(a)

    # comparison を家計簿科目のみに差し替え
    comparison["income_accounts"] = household_income
    comparison["expense_accounts"] = household_expense

    # 合計も再計算
    for i in range(12):
        comparison["income_totals"][i] = sum(a["months"][i] for a in household_income)
        comparison["expense_totals"][i] = sum(a["months"][i] for a in household_expense)

    biz_income_months = [biz_revenue_months[i] - biz_expense_months[i] for i in range(12)]

    return {
        "months": biz_income_months,
        "total": sum(biz_income_months),
    }
