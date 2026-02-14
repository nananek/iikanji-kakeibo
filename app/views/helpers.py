"""ビュー共通ヘルパー"""

from app.models.account import Account, AccountType


def get_grouped_accounts(user_id):
    """全科目をタイプごとにグルーピングしてJSON化可能なリストを返す"""
    account_types = AccountType.query.order_by(AccountType.display_order).all()
    accounts = (
        Account.query
        .filter_by(user_id=user_id, is_active=True)
        .order_by(Account.code)
        .all()
    )

    result = []
    for at in account_types:
        group = [a for a in accounts if a.account_type_id == at.id]
        if group:
            result.append({
                "type_code": at.code,
                "type_name": at.name,
                "normal_balance": at.normal_balance,
                "accounts": [
                    {"id": a.id, "code": a.code, "name": a.name}
                    for a in group
                ],
            })

    return result
