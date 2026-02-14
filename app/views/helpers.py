"""ビュー共通ヘルパー"""

import json
import os
import tempfile
import uuid

from app.models.account import Account, AccountType

# 一時ファイル保存先
_TEMP_DIR = os.path.join(tempfile.gettempdir(), "iikanji_import")
os.makedirs(_TEMP_DIR, exist_ok=True)


def save_import_data(data):
    """インポートデータを一時ファイルに保存し、キーを返す"""
    key = str(uuid.uuid4())
    path = os.path.join(_TEMP_DIR, key + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return key


def load_import_data(key):
    """キーからインポートデータを読み込む。なければNoneを返す"""
    if not key:
        return None
    path = os.path.join(_TEMP_DIR, key + ".json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_import_data(key):
    """インポートデータの一時ファイルを削除する"""
    if not key:
        return
    path = os.path.join(_TEMP_DIR, key + ".json")
    if os.path.exists(path):
        os.remove(path)


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
