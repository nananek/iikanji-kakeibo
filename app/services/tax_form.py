"""青色申告決算書の欄定義・マッピング管理"""

from app.extensions import db
from app.models.tax_form import TaxFormField, TaxFormMapping
from app.models.account import Account, AccountType


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
    """科目のマッピングを設定（既存があれば更新）"""
    existing = TaxFormMapping.query.filter_by(
        user_id=user_id, account_code=account_code,
    ).first()
    if existing:
        existing.field_id = field_id
    else:
        db.session.add(TaxFormMapping(
            user_id=user_id,
            account_code=account_code,
            field_id=field_id,
        ))
    db.session.flush()


def remove_mapping(user_id, account_code):
    """科目のマッピングを削除"""
    TaxFormMapping.query.filter_by(
        user_id=user_id, account_code=account_code,
    ).delete()
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


def save_mappings(user_id, mapping_data):
    """マッピングを一括保存。mapping_data: [{account_code, field_id}, ...]"""
    # 既存マッピングを全削除
    TaxFormMapping.query.filter_by(user_id=user_id).delete()
    db.session.flush()

    for item in mapping_data:
        code = item.get("account_code")
        field_id = item.get("field_id")
        if code and field_id:
            db.session.add(TaxFormMapping(
                user_id=user_id,
                account_code=code,
                field_id=int(field_id),
            ))
    db.session.flush()
