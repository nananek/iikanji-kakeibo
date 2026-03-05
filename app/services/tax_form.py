"""青色申告決算書の欄定義・マッピング管理"""

from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models.tax_form import TaxFormField, TaxFormMapping
from app.models.account import Account, AccountType
from app.models.journal import JournalEntry, JournalEntryLine


# 複数の決算書form_typeで共用可能な科目区分（B/S科目）
# 損益科目・純資産は1つのform_typeのみ
SHAREABLE_ACCOUNT_TYPES = {"asset", "liability"}


def get_form_fields(form_type="general"):
    """決算書の欄定義を取得（小計欄を含む）"""
    return (
        TaxFormField.query
        .filter_by(form_type=form_type)
        .order_by(TaxFormField.display_order)
        .all()
    )


def get_mappable_fields(form_type="general"):
    """マッピング可能な欄（小計欄を除く）を取得"""
    return (
        TaxFormField.query
        .filter_by(form_type=form_type, is_subtotal=False)
        .order_by(TaxFormField.display_order)
        .all()
    )


def get_user_mappings(user_id, form_type="general"):
    """ユーザーのマッピングを取得（field_id → [account_code, ...]）"""
    mappings = (
        TaxFormMapping.query
        .join(TaxFormField)
        .filter(
            TaxFormMapping.user_id == user_id,
            TaxFormField.form_type == form_type,
        )
        .all()
    )
    result = {}
    for m in mappings:
        result.setdefault(m.field_id, []).append(m.account_code)
    return result


def get_account_mapping(user_id, form_type="general"):
    """ユーザーのマッピングを取得（account_code → field_id）"""
    mappings = (
        TaxFormMapping.query
        .join(TaxFormField)
        .filter(
            TaxFormMapping.user_id == user_id,
            TaxFormField.form_type == form_type,
        )
        .all()
    )
    return {m.account_code: m.field_id for m in mappings}


def set_mapping(user_id, account_code, field_id):
    """科目のマッピングを設定。同一form_type内で既存があれば更新。

    損益・純資産科目の場合、他form_typeの同一科目マッピングを自動削除する。
    """
    field = db.session.get(TaxFormField, field_id)
    if not field:
        return
    # 同じform_type内の既存マッピングを検索
    existing = (
        TaxFormMapping.query
        .join(TaxFormField)
        .filter(
            TaxFormMapping.user_id == user_id,
            TaxFormMapping.account_code == account_code,
            TaxFormField.form_type == field.form_type,
        )
        .first()
    )
    if existing:
        existing.field_id = field_id
    else:
        db.session.add(TaxFormMapping(
            user_id=user_id,
            account_code=account_code,
            field_id=field_id,
        ))

    # 損益・純資産科目は他form_typeのマッピングを削除
    account = Account.query.filter_by(user_id=user_id, code=account_code).first()
    if account and account.account_type.code not in SHAREABLE_ACCOUNT_TYPES:
        stale = (
            TaxFormMapping.query
            .join(TaxFormField)
            .filter(
                TaxFormMapping.user_id == user_id,
                TaxFormMapping.account_code == account_code,
                TaxFormField.form_type != field.form_type,
            )
            .all()
        )
        for m in stale:
            db.session.delete(m)

    db.session.flush()


def remove_mapping(user_id, account_code, form_type="general"):
    """科目のマッピングを削除（form_typeスコープ）"""
    mappings = (
        TaxFormMapping.query
        .join(TaxFormField)
        .filter(
            TaxFormMapping.user_id == user_id,
            TaxFormMapping.account_code == account_code,
            TaxFormField.form_type == form_type,
        )
        .all()
    )
    for m in mappings:
        db.session.delete(m)
    db.session.flush()


def bulk_create_accounts(user_id, field_ids):
    """選択された決算書欄から勘定科目を一括作成し、マッピングも設定する。
    Returns: (created_count, skipped_codes)
    """
    type_map = {at.code: at.id for at in AccountType.query.all()}
    existing_codes = {
        a.code for a in Account.query.filter_by(user_id=user_id).all()
    }
    existing_mappings = {
        m.field_id
        for m in TaxFormMapping.query.filter_by(user_id=user_id).all()
    }

    fields = TaxFormField.query.filter(
        TaxFormField.id.in_(field_ids),
        TaxFormField.is_subtotal == False,  # noqa: E712
        TaxFormField.suggested_code.isnot(None),
    ).all()

    created = 0
    skipped = []

    # display_order の最大値を取得
    max_order = (
        db.session.query(db.func.max(Account.display_order))
        .filter_by(user_id=user_id)
        .scalar()
    ) or 0

    for field in fields:
        code = field.suggested_code
        if code in existing_codes:
            # 科目コードが既存の場合、マッピングだけ設定
            if field.id not in existing_mappings:
                set_mapping(user_id, code, field.id)
                existing_mappings.add(field.id)
            skipped.append(code)
            continue

        max_order += 10
        account = Account(
            user_id=user_id,
            account_type_id=type_map[field.account_type_code],
            code=code,
            name=field.name,
            description=f"青色決算書: {field.row_code} {field.name}",
            is_system=False,
            is_active=True,
            display_order=max_order,
        )
        db.session.add(account)
        existing_codes.add(code)

        set_mapping(user_id, code, field.id)
        existing_mappings.add(field.id)
        created += 1

    db.session.flush()
    return created, skipped


def get_business_account_codes(user_id):
    """事業科目コードのセットを返す（TaxFormMapping に紐づいている科目）"""
    codes = (
        db.session.query(TaxFormMapping.account_code)
        .filter_by(user_id=user_id)
        .all()
    )
    return {c[0] for c in codes}


def get_business_income(user_id, year, month=None):
    """事業所得を計算（事業収益 - 事業費用）。
    Returns: dict with revenue, expense, income, has_mappings
    """
    biz_codes = get_business_account_codes(user_id)
    if not biz_codes:
        return {"revenue": 0, "expense": 0, "income": 0, "has_mappings": False}

    start = date(year, month or 1, 1)
    if month:
        if month == 12:
            end = date(year + 1, 1, 1)
        else:
            end = date(year, month + 1, 1)
    else:
        end = date(year + 1, 1, 1)

    revenue_type = AccountType.query.filter_by(code="revenue").first()
    expense_type = AccountType.query.filter_by(code="expense").first()

    def _sum(type_id, amount_col):
        return (
            db.session.query(
                func.coalesce(func.sum(amount_col), 0)
            )
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .join(Account, db.and_(
                Account.user_id == JournalEntryLine.account_user_id,
                Account.code == JournalEntryLine.account_code,
            ))
            .filter(
                JournalEntry.user_id == user_id,
                JournalEntry.date >= start,
                JournalEntry.date < end,
                JournalEntry.source != "closing",
                Account.account_type_id == type_id,
                Account.code.in_(biz_codes),
            )
            .scalar()
        ) or 0

    biz_revenue = 0
    biz_expense = 0
    if revenue_type:
        cr = _sum(revenue_type.id, JournalEntryLine.credit_amount)
        dr = _sum(revenue_type.id, JournalEntryLine.debit_amount)
        biz_revenue = int(cr - dr)
    if expense_type:
        dr = _sum(expense_type.id, JournalEntryLine.debit_amount)
        cr = _sum(expense_type.id, JournalEntryLine.credit_amount)
        biz_expense = int(dr - cr)

    return {
        "revenue": biz_revenue,
        "expense": biz_expense,
        "income": biz_revenue - biz_expense,
        "has_mappings": True,
    }


def get_tax_form_report(user_id, year, form_type="general"):
    """決算書レポート用データ。各欄の金額を集計する。"""
    fields = get_form_fields(form_type)
    field_mappings = get_user_mappings(user_id, form_type)

    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)

    # 全科目の年間発生額を一括取得
    rows = (
        db.session.query(
            Account.code,
            Account.account_type_id,
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0).label("total_debit"),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0).label("total_credit"),
        )
        .join(JournalEntryLine, db.and_(
            JournalEntryLine.account_user_id == Account.user_id,
            JournalEntryLine.account_code == Account.code,
        ))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            JournalEntry.date >= start,
            JournalEntry.date < end,
            JournalEntry.source != "closing",
        )
        .group_by(Account.code, Account.account_type_id)
        .all()
    )

    # 科目区分の正常残高
    type_map = {at.id: at for at in AccountType.query.all()}
    amounts = {}
    for row in rows:
        at = type_map.get(row.account_type_id)
        if not at:
            continue
        if at.normal_balance == "debit":
            amounts[row.code] = int(row.total_debit) - int(row.total_credit)
        else:
            amounts[row.code] = int(row.total_credit) - int(row.total_debit)

    # B/S科目: 全期間残高が必要（期首残高を含む）
    bs_rows = (
        db.session.query(
            Account.code,
            Account.account_type_id,
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0).label("total_debit"),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0).label("total_credit"),
        )
        .join(JournalEntryLine, db.and_(
            JournalEntryLine.account_user_id == Account.user_id,
            JournalEntryLine.account_code == Account.code,
        ))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            JournalEntry.date < end,
        )
        .group_by(Account.code, Account.account_type_id)
        .all()
    )

    bs_amounts = {}  # 期末残高
    bs_opening = {}  # 期首残高
    for row in bs_rows:
        at = type_map.get(row.account_type_id)
        if not at or at.code not in ("asset", "liability", "equity"):
            continue
        if at.normal_balance == "debit":
            bs_amounts[row.code] = int(row.total_debit) - int(row.total_credit)
        else:
            bs_amounts[row.code] = int(row.total_credit) - int(row.total_debit)

    # 期首残高: 期末 - 当年P/L発生
    bs_opening_rows = (
        db.session.query(
            Account.code,
            Account.account_type_id,
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0).label("total_debit"),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0).label("total_credit"),
        )
        .join(JournalEntryLine, db.and_(
            JournalEntryLine.account_user_id == Account.user_id,
            JournalEntryLine.account_code == Account.code,
        ))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            JournalEntry.date < start,
        )
        .group_by(Account.code, Account.account_type_id)
        .all()
    )
    for row in bs_opening_rows:
        at = type_map.get(row.account_type_id)
        if not at or at.code not in ("asset", "liability", "equity"):
            continue
        if at.normal_balance == "debit":
            bs_opening[row.code] = int(row.total_debit) - int(row.total_credit)
        else:
            bs_opening[row.code] = int(row.total_credit) - int(row.total_debit)

    # 各欄に金額を割り当て
    field_data = []
    for field in fields:
        codes = field_mappings.get(field.id, [])
        if field.page == 4 and not field.is_subtotal:
            # B/S欄: 期首・期末
            opening = sum(bs_opening.get(c, 0) for c in codes)
            closing = sum(bs_amounts.get(c, 0) for c in codes)
            field_data.append({
                "field": field,
                "codes": codes,
                "amount": closing,
                "opening": opening,
            })
        else:
            # P/L欄: 当年発生額
            amount = sum(amounts.get(c, 0) for c in codes)
            field_data.append({
                "field": field,
                "codes": codes,
                "amount": amount,
                "opening": None,
            })

    # 小計の計算
    _compute_subtotals(field_data)

    return field_data


def _compute_subtotals(field_data):
    """小計欄の金額を計算"""
    # セクションごとに集計
    section_totals = {}
    for item in field_data:
        f = item["field"]
        if f.is_subtotal:
            continue
        key = (f.page, f.section)
        section_totals.setdefault(key, {"amount": 0, "opening": 0})
        section_totals[key]["amount"] += item["amount"]
        if item["opening"] is not None:
            section_totals[key]["opening"] += item["opening"]

    # P1 の計算ロジック
    revenue = section_totals.get((1, "revenue"), {"amount": 0})["amount"]
    cos_total = section_totals.get((1, "cost_of_sales"), {"amount": 0})["amount"]
    expenses_total = section_totals.get((1, "expenses"), {"amount": 0})["amount"]

    # 各小計の値を代入
    for item in field_data:
        f = item["field"]
        if not f.is_subtotal:
            continue

        if f.section == "cost_of_sales" and f.row_code == "4":
            # 小計 = 期首棚卸 + 仕入
            item["amount"] = cos_total
        elif f.section == "cost_of_sales" and f.row_code == "6":
            # 差引原価 = 小計 - 期末棚卸
            ending_inv = 0
            for d in field_data:
                if d["field"].row_code == "5" and d["field"].section == "cost_of_sales":
                    ending_inv = d["amount"]
            item["amount"] = cos_total - ending_inv
        elif f.section == "cost_of_sales" and f.row_code == "7":
            # 差引金額 = 売上 - 差引原価
            cost_of_sales = 0
            for d in field_data:
                if d["field"].row_code == "6" and d["field"].section == "cost_of_sales":
                    cost_of_sales = d["amount"]
            item["amount"] = revenue - cost_of_sales
        elif f.section == "expenses" and f.row_code == "30":
            item["amount"] = expenses_total
        elif f.section == "income" and f.row_code == "31":
            # 差引金額 = 売上 - 原価 - 経費
            cost_of_sales = 0
            for d in field_data:
                if d["field"].row_code == "6" and d["field"].section == "cost_of_sales":
                    cost_of_sales = d["amount"]
            item["amount"] = revenue - cost_of_sales - expenses_total
        elif f.section == "income" and f.row_code == "35":
            # 青色申告特別控除前の所得金額
            gross = 0
            for d in field_data:
                if d["field"].row_code == "31" and d["field"].section == "income":
                    gross = d["amount"]
            # 専従者給与・引当金等は個別欄から取得
            senshusha = 0
            kurimodoshi = 0
            kuriire = 0
            for d in field_data:
                if d["field"].row_code == "32":
                    senshusha = d["amount"]
                elif d["field"].row_code == "33":
                    kurimodoshi = d["amount"]
                elif d["field"].row_code == "34":
                    kuriire = d["amount"]
            item["amount"] = gross - senshusha + kurimodoshi - kuriire
        elif f.section == "income" and f.row_code == "37":
            # 所得金額 = 控除前 - 控除額
            before = 0
            deduction = 0
            for d in field_data:
                if d["field"].row_code == "35":
                    before = d["amount"]
                elif d["field"].row_code == "36":
                    deduction = d["amount"]
            item["amount"] = before - deduction
        elif f.section == "bs_assets" and f.row_code == "AT":
            key = (4, "bs_assets")
            item["amount"] = section_totals.get(key, {"amount": 0})["amount"]
            item["opening"] = section_totals.get(key, {"opening": 0})["opening"]
        elif f.section == "bs_liabilities" and f.row_code == "LT":
            key = (4, "bs_liabilities")
            item["amount"] = section_totals.get(key, {"amount": 0})["amount"]
            item["opening"] = section_totals.get(key, {"opening": 0})["opening"]


def save_mappings(user_id, mapping_data, form_type="general"):
    """マッピングを一括保存（form_typeスコープ）。mapping_data: [{account_code, field_id}, ...]

    損益・純資産科目は1つのform_typeのみに所属できる。
    他のform_typeに同じ科目があれば自動削除される。
    資産・負債科目は複数form_typeで共用可能。
    """
    # 該当form_typeの既存マッピングのみ削除
    existing = (
        TaxFormMapping.query
        .join(TaxFormField)
        .filter(
            TaxFormMapping.user_id == user_id,
            TaxFormField.form_type == form_type,
        )
        .all()
    )
    for m in existing:
        db.session.delete(m)
    db.session.flush()

    # 科目区分マップ
    account_types = {
        a.code: a.account_type.code
        for a in Account.query.filter_by(user_id=user_id).all()
    }

    exclusive_codes = []  # 排他制御が必要な科目コード
    for item in mapping_data:
        code = item.get("account_code")
        field_id = item.get("field_id")
        if code and field_id:
            db.session.add(TaxFormMapping(
                user_id=user_id,
                account_code=code,
                field_id=int(field_id),
            ))
            # 損益・純資産科目は他form_typeから削除
            at_code = account_types.get(code)
            if at_code and at_code not in SHAREABLE_ACCOUNT_TYPES:
                exclusive_codes.append(code)

    if exclusive_codes:
        # 他form_typeの同一科目マッピングを削除
        stale = (
            TaxFormMapping.query
            .join(TaxFormField)
            .filter(
                TaxFormMapping.user_id == user_id,
                TaxFormMapping.account_code.in_(exclusive_codes),
                TaxFormField.form_type != form_type,
            )
            .all()
        )
        for m in stale:
            db.session.delete(m)

    db.session.flush()
