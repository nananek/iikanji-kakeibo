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


# #338 Phase R-1: get_business_income (事業科目の平文 debit/credit を SQL SUM して
# 事業所得を計算していた) を撤去した。サーバが平文金額を読む経路だったため。事業所得は
# クライアントが accounts_meta の is_business 科目に computeProfitLoss を流用して算出する
# (profit_loss_renderer.mjs)。get_business_account_codes (accounts/TaxFormMapping のみ
# 参照、平文 line 非依存) は is_business 判定用に維持。



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
