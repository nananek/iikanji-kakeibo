"""ビュー共通ヘルパー"""

import json
import os
import tempfile
import uuid
from datetime import date, datetime
from urllib.parse import urlparse

from app.models.account import Account, AccountType

DEADLINE_DAYS = 67  # 電帳法: 約2ヶ月+7営業日


_UNSAFE_ERROR_TOKENS = (
    "Traceback",
    "/app/",
    'File "',
    " line ",
    "<class ",
    "psycopg",
    "sqlalchemy",
)


def safe_user_error(exc: Exception, fallback: str = "処理に失敗しました") -> str:
    """例外メッセージを API レスポンスに含めるときの sanitizer。

    業務ロジックが投げた ValueError 等の短い説明文はユーザー向けに残しつつ、
    スタックトレース由来の文字列・内部パス・長文は fallback で置換する。
    フル例外は呼び出し側で logger に残すこと。
    """
    msg = exc.args[0] if getattr(exc, "args", None) else ""
    if not isinstance(msg, str) or not msg:
        return fallback
    if len(msg) > 200 or "\n" in msg or "\r" in msg:
        return fallback
    if any(tok in msg for tok in _UNSAFE_ERROR_TOKENS):
        return fallback
    return msg


def is_safe_internal_path(target) -> bool:
    """target がアプリ内部の相対パス（'/foo/bar'）であれば True を返す。

    オープンリダイレクト対策として redirect() に渡す前に必ず使う。
    以下は全て False:
    - 空・None・str 以外
    - 先頭が '/' でない
    - '//evil.com' '/\\evil.com' などのプロトコル相対 / バックスラッシュ
    - urlparse で scheme / netloc が検出される
    """
    if not isinstance(target, str) or not target:
        return False
    if not target.startswith("/"):
        return False
    if target.startswith("//") or target.startswith("/\\"):
        return False
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    return True


def maybe_clear_pending_recovery(user, session_obj):
    """リカバリログイン後の「強制復旧フロー」終了判定。

    パスキーが 1 本以上登録され、かつ新規リカバリコードも生成された場合に
    `pending_recovery_action` セッション flag をクリアする。両方達成しないと
    残ったまま (`before_request` で他ページがブロックされる)。
    """
    if not session_obj.get("pending_recovery_action"):
        return
    from app.models.webauthn import WebAuthnCredential
    passkey_count = WebAuthnCredential.query.filter_by(user_id=user.id).count()
    if passkey_count >= 1 and user.has_active_recovery_code:
        session_obj.pop("pending_recovery_action", None)
        session_obj.pop("pending_recovery_user_id", None)


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
