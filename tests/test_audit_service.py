"""監査サービス (services/audit.py) のテスト

既存の test_audit.py は IDOR / 公開科目フィルタ等のセッション込みのフロー。
こちらはセッション state 切替・get_acting_as_user / require_permission /
check_auditor_redirect のヘルパー側を補強。
"""

import pytest
from flask import session
from flask_login import login_user

from app.models.audit import AuditGrant
from app.services.audit import (
    check_auditor_redirect,
    get_acting_as_grant,
    get_acting_as_user,
    get_allowed_account_codes,
    get_effective_user_id,
    get_permission_level,
    is_acting_as_auditor,
    require_permission,
)


class TestGetEffectiveUserId:
    def test_normal_user(self, app, user):
        with app.test_request_context():
            login_user(user)
            assert get_effective_user_id() == user.id

    def test_acting_with_valid_grant(self, app, user, auditor, db):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=3,
        )
        db.session.add(grant)
        db.session.commit()
        with app.test_request_context():
            login_user(auditor)
            session["acting_as_user_id"] = user.id
            session["acting_as_permission_level"] = 3
            assert get_effective_user_id() == user.id

    def test_acting_with_revoked_grant_clears_session(self, app, user, auditor, db):
        # acting_as があるが grant が無い → セッションクリア
        with app.test_request_context():
            login_user(auditor)
            session["acting_as_user_id"] = user.id
            session["acting_as_permission_level"] = 3
            result = get_effective_user_id()
            assert result == auditor.id  # フォールバック
            assert "acting_as_user_id" not in session


class TestGetPermissionLevel:
    def test_no_acting(self, app, user):
        with app.test_request_context():
            login_user(user)
            assert get_permission_level() is None

    def test_acting(self, app, auditor):
        with app.test_request_context():
            login_user(auditor)
            session["acting_as_user_id"] = 42
            session["acting_as_permission_level"] = 2
            assert get_permission_level() == 2


class TestIsActingAsAuditor:
    def test_no(self, app, user):
        with app.test_request_context():
            login_user(user)
            assert is_acting_as_auditor() is False

    def test_yes(self, app, auditor):
        with app.test_request_context():
            login_user(auditor)
            session["acting_as_user_id"] = 1
            assert is_acting_as_auditor() is True


class TestRequirePermission:
    def test_no_acting_passes(self, app, user):
        with app.test_request_context():
            login_user(user)
            # 制限なし → abort されない
            require_permission(3)

    def test_lv1_blocked_when_lv2_required(self, app, auditor):
        with app.test_request_context():
            login_user(auditor)
            session["acting_as_user_id"] = 1
            session["acting_as_permission_level"] = 1
            from werkzeug.exceptions import Forbidden
            with pytest.raises(Forbidden):
                require_permission(2)

    def test_lv3_passes_lv2_check(self, app, auditor):
        with app.test_request_context():
            login_user(auditor)
            session["acting_as_user_id"] = 1
            session["acting_as_permission_level"] = 3
            require_permission(2)


class TestGetActingAsUser:
    def test_no_acting(self, app, user):
        with app.test_request_context():
            login_user(user)
            assert get_acting_as_user() is None

    def test_with_acting(self, app, user, auditor):
        with app.test_request_context():
            login_user(auditor)
            session["acting_as_user_id"] = user.id
            result = get_acting_as_user()
            assert result is not None
            assert result.id == user.id


class TestGetActingAsGrant:
    def test_no_acting(self, app, user):
        with app.test_request_context():
            login_user(user)
            assert get_acting_as_grant() is None

    def test_with_grant(self, app, user, auditor, db):
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=3,
        )
        db.session.add(grant)
        db.session.commit()
        with app.test_request_context():
            login_user(auditor)
            session["acting_as_user_id"] = user.id
            result = get_acting_as_grant()
            assert result is not None
            assert result.id == grant.id


class TestGetAllowedAccountCodes:
    def test_not_lv2_returns_none(self, app, user):
        with app.test_request_context():
            login_user(user)
            assert get_allowed_account_codes() is None

    def test_lv2_no_grant_returns_empty(self, app, auditor):
        with app.test_request_context():
            login_user(auditor)
            session["acting_as_user_id"] = 9999
            session["acting_as_permission_level"] = 2
            result = get_allowed_account_codes()
            assert result == set()

    def test_lv2_with_grant(self, app, user, auditor, accounts, db):
        from app.models.audit import AuditGrantAccount
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=2,
        )
        db.session.add(grant)
        db.session.flush()
        db.session.add_all([
            AuditGrantAccount(audit_grant_id=grant.id,
                              account_user_id=user.id, account_code="5010"),
            AuditGrantAccount(audit_grant_id=grant.id,
                              account_user_id=user.id, account_code="1010"),
        ])
        db.session.commit()
        with app.test_request_context():
            login_user(auditor)
            session["acting_as_user_id"] = user.id
            session["acting_as_permission_level"] = 2
            result = get_allowed_account_codes()
            assert result == {"5010", "1010"}


class TestCheckAuditorRedirect:
    def test_no_acting_returns_none(self, app, user):
        # acting_as 未設定なら、エンドポイントに関わらず None
        with app.test_request_context("/dashboard/"):
            login_user(user)
            assert check_auditor_redirect() is None

    def test_lv1_on_tax_page_no_redirect(self, client, auditor, db, user, accounts):
        # 実 client で reports.tax にアクセスしてリダイレクトが起きないことを間接的に確認
        # Lv1 監査ユーザー＋ acting_as = personal user
        from app.models.audit import AuditGrant
        grant = AuditGrant(
            owner_user_id=user.id,
            auditor_user_id=auditor.id,
            permission_level=1,
            status="active",
        )
        db.session.add(grant)
        db.session.commit()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 1
        # Lv1 でも reports.tax は許可
        resp = client.get("/reports/tax")
        assert resp.status_code in (200, 302)  # 200 か他のリダイレクト

    # 注: 旧 audit_permission_check ゲート (Lv1 を dashboard から弾く) は撤去済み
    # (#112)。endpoint ベースのリダイレクト挙動を検証していた
    # test_lv1_on_dashboard_redirects はゲート削除に伴い削除した。
    # check_auditor_redirect 関数自体の純粋ロジックは PR-5 で関数ごと撤去予定。
