"""ビュー共通ヘルパー"""

import json
import os
import tempfile
import uuid
from datetime import date, datetime

from app.models.account import Account, AccountType

DEADLINE_DAYS = 67  # 電帳法: 約2ヶ月+7営業日


def check_deadline(receipt_date, uploaded_date):
    """入力期限チェック。期限超過なら True を返す。"""
    if not receipt_date or not uploaded_date:
        return False
    if isinstance(uploaded_date, datetime):
        uploaded_date = uploaded_date.date()
    if isinstance(receipt_date, datetime):
        receipt_date = receipt_date.date()
    return (uploaded_date - receipt_date).days > DEADLINE_DAYS

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


def _safe_temp_path(key):
    """キーから安全な一時ファイルパスを返す。パストラバーサルならNone"""
    if not key:
        return None
    path = os.path.join(_TEMP_DIR, key + ".json")
    resolved = os.path.realpath(path)
    if not resolved.startswith(os.path.realpath(_TEMP_DIR) + os.sep):
        return None
    return resolved


def load_import_data(key):
    """キーからインポートデータを読み込む。なければNoneを返す"""
    path = _safe_temp_path(key)
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_import_data(key):
    """インポートデータの一時ファイルを削除する"""
    path = _safe_temp_path(key)
    if not path:
        return
    if os.path.exists(path):
        os.remove(path)


def get_grouped_accounts(user_id, allowed_account_codes=None):
    """全科目をタイプごとにグルーピングしてJSON化可能なリストを返す

    allowed_account_codes: Noneなら全科目、setなら指定codeの科目のみ
    """
    account_types = AccountType.query.order_by(AccountType.display_order).all()
    accounts = (
        Account.query
        .filter_by(user_id=user_id, is_active=True)
        .order_by(Account.code)
        .all()
    )

    if allowed_account_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_account_codes]

    result = []
    for at in account_types:
        group = [a for a in accounts if a.account_type_id == at.id]
        if group:
            result.append({
                "type_code": at.code,
                "type_name": at.name,
                "normal_balance": at.normal_balance,
                "accounts": [
                    {"code": a.code, "name": a.name}
                    for a in group
                ],
            })

    return result
