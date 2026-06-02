"""監査用アカウントのヘルパー関数"""

from flask import session, abort
from flask_login import current_user

from app.extensions import db
from app.models.account import Account
from app.models.audit import AuditGrant, AuditGrantAccount


def get_effective_user_id():
    """代理閲覧中はクライアントのuser_id、それ以外はcurrent_user.id"""
    acting_as = session.get("acting_as_user_id")
    if acting_as:
        # グラントがまだ有効か確認
        grant = AuditGrant.query.filter_by(
            owner_user_id=acting_as,
            auditor_user_id=current_user.id,
        ).first()
        if not grant:
            # 取り消されたのでセッションをクリア
            session.pop("acting_as_user_id", None)
            session.pop("acting_as_permission_level", None)
            return current_user.id
        return acting_as
    return current_user.id


def get_permission_level():
    """代理閲覧中は権限レベル(1/2/3)、それ以外はNone"""
    if not session.get("acting_as_user_id"):
        return None
    return session.get("acting_as_permission_level")


def is_acting_as_auditor():
    """代理閲覧中かどうか"""
    return session.get("acting_as_user_id") is not None


def require_permission(level):
    """代理閲覧中に権限レベルが不足していればabort(403)"""
    perm = get_permission_level()
    if perm is not None and perm < level:
        abort(403)


def get_acting_as_user():
    """代理閲覧中のUserオブジェクト"""
    from app.models.user import User
    acting_as = session.get("acting_as_user_id")
    if acting_as:
        return db.session.get(User, acting_as)
    return None


def get_acting_as_grant():
    """代理閲覧中のAuditGrantオブジェクト"""
    acting_as = session.get("acting_as_user_id")
    if not acting_as:
        return None
    return AuditGrant.query.filter_by(
        owner_user_id=acting_as,
        auditor_user_id=current_user.id,
    ).first()


def get_allowed_account_codes():
    """Lv2の場合の公開科目コードセット。Lv2でなければNone"""
    perm = get_permission_level()
    if perm != 2:
        return None
    grant = get_acting_as_grant()
    if not grant:
        return set()
    return {
        ga.account_code
        for ga in AuditGrantAccount.query.filter_by(audit_grant_id=grant.id).all()
    }


def is_entry_locked_for_auditor(entry, allowed_account_codes):
    """監査者が伝票を編集・削除できるかチェック

    伝票の明細行に非公開科目が1つでもあればTrue
    """
    if allowed_account_codes is None:
        return False
    for line in entry.lines:
        if line.account_code not in allowed_account_codes:
            return True
    return False


def get_proprietor_account_code(user_id):
    """事業主科目のコードを返す"""
    account = Account.query.filter_by(
        user_id=user_id, system_role="proprietor"
    ).first()
    return account.code if account else None


def check_auditor_redirect():
    """Lv1監査者がtax以外にアクセスしたときリダイレクト用URLを返す。不要ならNone"""
    from flask import request
    perm = get_permission_level()
    if perm is None:
        return None
    endpoint = request.endpoint or ""
    # Lv1: reports.tax 以外はリダイレクト (E3-F-4c で medical_csv 撤去)
    if perm == 1 and endpoint not in ("reports.tax", "auditor.exit_acting"):
        return True
    return None


def mask_account_name(account_name, account_code, allowed_account_codes):
    """Lv2監査者向け: 非公開科目名を「事業主」に差し替え"""
    if allowed_account_codes is None:
        return account_name
    if account_code in allowed_account_codes:
        return account_name
    return "事業主"
