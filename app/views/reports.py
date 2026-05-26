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
    get_income_expense_summary,
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
    """確定申告用集計 (tax_summary / medical_summary 共にクライアント描画)。

    - tax_summary: Phase E3-F-3f でクライアント完結化済
    - medical_summary: Phase E3-F-3g でクライアント完結化
    """
    year = request.args.get("year", date.today().year, type=int)
    user_id = get_effective_user_id()

    allowed_codes = get_allowed_account_codes()
    accounts = (
        Account.query
        .filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    if allowed_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_codes]

    # tax_summary 用 (medical/resident_tax は除外)
    tax_accounts_meta = {
        a.code: {
            "name": mask_account_name(a.name, a.code, allowed_codes),
            "tax_category": a.tax_category,
        }
        for a in accounts
        if a.tax_category is not None and a.tax_category not in ("medical", "resident_tax")
    }
    # medical_summary 用 (medical 科目のみ)
    medical_accounts_meta = {
        a.code: {
            "name": mask_account_name(a.name, a.code, allowed_codes),
            "tax_category": a.tax_category,
        }
        for a in accounts
        if a.tax_category == "medical"
    }

    return render_template(
        "reports/tax.html",
        year=year,
        tax_accounts_meta=tax_accounts_meta,
        medical_accounts_meta=medical_accounts_meta,
        effective_user_id=user_id,
    )


@bp.route("/tax/medical-csv")
@login_required
def medical_csv():
    """医療費集計フォーム Ver 3.1 準拠CSVダウンロード。

    E2EE 化が進むと get_medical_summary は復号できなくなるため、
    clientside CSV 生成に置き換える follow-up が必要 (#221 系)。
    """
    from app.services.tax import get_medical_summary

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
    """総勘定元帳 (クライアント描画)。サーバ集計は撤去済 (Phase E3-F-3e)。
    クライアントが /api/v1/journals から MK 復号 → computeLedger →
    DOM 構築まで完結する。

    entries_meta (entry_id → {is_readonly, voucher_id, entry_number}) は
    JournalEntry / Voucher テーブルの非暗号化カラム経由でサーバ側で算出。
    carry_forward (前期繰越) は当面 0。BCB 統合後 follow-up #221 で復元。
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
    accounts = [a for a in accounts if a.is_active or (a.deactivated_year and a.deactivated_year >= year)]
    if allowed_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_codes]

    grouped_accounts = {}
    for at in account_types:
        group = [a for a in accounts if a.account_type_id == at.id]
        if group:
            grouped_accounts[at] = group

    # accounts_meta は client が name / normal_balance / type を参照する
    accounts_meta = {
        a.code: {
            "name": mask_account_name(a.name, a.code, allowed_codes),
            "type": a.account_type.code,
            "normal_balance": a.account_type.normal_balance,
        }
        for a in accounts
    }

    selected_account = None
    entries_meta = {}

    if account_code:
        # Lv2: 非公開科目アクセスを 403 で遮断
        if allowed_codes is not None and account_code not in allowed_codes:
            from flask import abort
            abort(403)

        selected_account = Account.query.filter_by(
            user_id=user_id, code=account_code,
        ).first()

        if selected_account:
            # 該当科目を含む journal_entries の id を取得
            entry_ids = [
                eid for (eid,) in (
                    db.session.query(JournalEntryLine.journal_entry_id)
                    .filter(
                        JournalEntryLine.account_user_id == user_id,
                        JournalEntryLine.account_code == account_code,
                    )
                    .distinct()
                    .all()
                )
            ]
            # defence-in-depth: entry_ids は account_user_id でフィルタ済だが
                # JournalEntry にも user_id 制約を重ねる
            entry_objs = {
                eo.id: eo
                for eo in JournalEntry.query.filter(
                    JournalEntry.id.in_(entry_ids),
                    JournalEntry.user_id == user_id,
                ).all()
            }
            from app.models.voucher import Voucher
            voucher_map = {}
            voucher_rows = Voucher.active().filter(
                Voucher.journal_entry_id.in_(entry_ids)
            ).all()
            for v in voucher_rows:
                voucher_map.setdefault(v.journal_entry_id, []).append(v)
            for eid, eo in entry_objs.items():
                is_readonly = bool(
                    check_entry_modifiable(user_id, eo) is not None
                    or is_entry_locked_for_owner(user_id, eo)
                )
                vlist = voucher_map.get(eid, [])
                entries_meta[eid] = {
                    "is_readonly": is_readonly,
                    "voucher_id": vlist[0].id if vlist else None,
                    "entry_number": eo.entry_number,
                }

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
        accounts_meta=accounts_meta,
        entries_meta=entries_meta,
        effective_user_id=user_id,
        all_grouped_accounts=all_grouped,
    )


@bp.route("/tax-form")
@login_required
def tax_form_report():
    """青色申告決算書 (クライアント描画)。サーバ集計は撤去済 (Phase E3-F-3h)。

    クライアントが min_year..year を MK で復号して P/L 当年発生額 / B/S 期末 /
    B/S 期首を組み立て、`composeTaxFormView` で field_data を計算する。
    """
    from app.services.tax_form import get_form_fields, get_user_mappings

    year = request.args.get("year", date.today().year, type=int)
    form_type = request.args.get("form_type", "general")
    if form_type not in ("general", "real_estate"):
        form_type = "general"
    user_id = get_effective_user_id()

    fields = get_form_fields(form_type)
    field_mappings = get_user_mappings(user_id, form_type)

    # 仕訳累計のため最古年度を取得 (BS 期首/期末計算用)。仕訳ゼロなら None
    min_year = (
        db.session.query(func.min(JournalEntry.fiscal_year))
        .filter(JournalEntry.user_id == user_id)
        .scalar()
    )

    # form_structure JSON: fields は配列、mappings は {field_id: [code, ...]}
    form_structure = {
        "fields": [
            {
                "id": f.id,
                "page": f.page,
                "section": f.section,
                "row_code": f.row_code,
                "name": f.name,
                "is_subtotal": bool(f.is_subtotal),
                "is_user_defined": bool(f.is_user_defined),
                "display_order": f.display_order,
            }
            for f in fields
        ],
        "mappings": {
            int(field_id): list(codes)
            for field_id, codes in field_mappings.items()
        },
    }

    # accounts_meta: normal_balance を client が必要とする
    allowed_codes = get_allowed_account_codes()
    accounts = (
        Account.query
        .filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    if allowed_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_codes]
    accounts_meta = {
        a.code: {
            "type": a.account_type.code,
            "normal_balance": a.account_type.normal_balance,
            "name": mask_account_name(a.name, a.code, allowed_codes),
        }
        for a in accounts
    }

    return render_template(
        "reports/tax_form.html",
        year=year,
        form_type=form_type,
        form_structure=form_structure,
        accounts_meta=accounts_meta,
        min_year=min_year,
        effective_user_id=user_id,
    )


@bp.route("/monthly")
@login_required
def monthly():
    """月次比較レポート (クライアント描画)。サーバ集計は撤去済 (Phase E3-F-3d)。
    accounts_meta (cost_type / is_business 含む) と projection (現状サーバ
    計算) を JSON で渡す。
    """
    from app.services.tax_form import get_business_account_codes

    year = request.args.get("year", date.today().year, type=int)
    user_id = get_effective_user_id()

    today = date.today()
    current_month = today.month if year == today.year else None

    # projection (当月着地予想) は当面サーバ側で計算する (clientside 化は follow-up)。
    # ただし render に必要な comparison を tax service 経由で取り直す。
    projection = None
    if current_month and today.day <= \
            __import__("calendar").monthrange(year, today.month)[1]:
        from app.services.tax import get_monthly_comparison as _gmc
        comparison = _gmc(user_id, year)
        method = current_user.get_pref("projection_method", "pro_rata")
        projection = get_month_projection(
            user_id, year, today.month, comparison, method=method,
        )

    allowed_codes = get_allowed_account_codes()
    accounts = (
        Account.query
        .filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    if allowed_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_codes]

    biz_codes = get_business_account_codes(user_id)

    accounts_meta = {
        a.code: {
            "type": a.account_type.code,
            "name": mask_account_name(a.name, a.code, allowed_codes),
            "cost_type": a.cost_type or "occasional",
            "is_business": a.code in biz_codes,
        }
        for a in accounts
    }

    return render_template(
        "reports/monthly.html",
        year=year,
        current_month=current_month,
        accounts_meta=accounts_meta,
        projection=projection,
        effective_user_id=user_id,
        tax_form_url=None,  # Jinja で url_for を呼ぶ
    )
