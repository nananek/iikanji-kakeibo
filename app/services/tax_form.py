"""青色申告決算書の欄定義・マッピング管理"""

from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models.tax_form import TaxFormField, TaxFormMapping
from app.models.account import Account, AccountType
from app.models.journal import JournalEntry, JournalEntryLine


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
    """科目のマッピングを設定（既存があれば更新）。

    1科目は1つのform_typeにのみ所属できる。
    他form_typeの同一科目マッピングは自動削除される。
    """
    # 既存マッピングを全form_typeから削除
    TaxFormMapping.query.filter_by(
        user_id=user_id, account_code=account_code,
    ).delete()
    db.session.add(TaxFormMapping(
        user_id=user_id,
        account_code=account_code,
        field_id=field_id,
    ))
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



def save_mappings(user_id, mapping_data, form_type="general"):
    """マッピングを一括保存（form_typeスコープ）。mapping_data: [{account_code, field_id}, ...]

    該当form_typeの既存マッピングを全削除して再作成する。
    新たにマッピングする科目が他form_typeに存在する場合も削除される
    （1科目は1つのform_typeにのみ所属可能）。
    """
    # 該当form_typeの既存マッピングを削除
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

    # 新しいマッピング対象の科目コードを収集
    new_items = []
    for item in mapping_data:
        code = item.get("account_code")
        field_id = item.get("field_id")
        if code and field_id:
            new_items.append((code, int(field_id)))

    # 他form_typeの同一科目マッピングを先に削除（排他制御）
    new_codes = [code for code, _ in new_items]
    if new_codes:
        stale = (
            TaxFormMapping.query
            .join(TaxFormField)
            .filter(
                TaxFormMapping.user_id == user_id,
                TaxFormMapping.account_code.in_(new_codes),
                TaxFormField.form_type != form_type,
            )
            .all()
        )
        for m in stale:
            db.session.delete(m)
        db.session.flush()

    for code, field_id in new_items:
        db.session.add(TaxFormMapping(
            user_id=user_id,
            account_code=code,
            field_id=field_id,
        ))
    db.session.flush()
