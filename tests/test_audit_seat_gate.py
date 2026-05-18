"""監査枠 (audit_seat) エンタイトルメントゲートのテスト (Phase 2 #67)。

- `/settings/audit/add` で `AuditGrant` を作成する際、auditor または
  owner のいずれかが `audit_seat` を持っている必要がある
- `audit_permission_check` フックで代理閲覧中もエンタイトルメントを再検証

セルフホストモード (`BILLING_BACKEND=unlimited`, デフォルト) では
`UnlimitedBillingClient` が常に True を返すため通過する。
"""

import pytest

from app.models.audit import AuditGrant
from app.services.entitlement import UnlimitedBillingClient


# --- ヘルパー --------------------------------------------------------------


def _patch_billing(monkeypatch, *, auditor_ok: bool, owner_ok: bool):
    """auditor / owner に応じた has_entitlement の返り値を制御するクライアントを差し込む。

    `audit_seat` のみ評価対象。他の feature_key は True で通す。
    """
    class Client(UnlimitedBillingClient):
        def has_entitlement(self, user, feature_key):
            if feature_key != "audit_seat":
                return True
            if user is None:
                return False
            if user.user_type == "auditor":
                return auditor_ok
            return owner_ok

    from app.services import entitlement as ent
    monkeypatch.setattr(ent, "get_billing_client", lambda: Client())


# --- /settings/audit/add ---------------------------------------------------


class TestAuditAddGate:
    """AuditGrant 作成時の有償ゲート"""

    def test_unlimited_allows_creation(self, db, logged_in_client, user, accounts, auditor):
        """デフォルト (unlimited) では従来通り作成可能"""
        resp = logged_in_client.post(
            "/settings/audit/add",
            data={"username": auditor.username, "permission_level": "3"},
        )
        assert resp.status_code in (302, 303)
        assert AuditGrant.query.filter_by(
            owner_user_id=user.id, auditor_user_id=auditor.id
        ).count() == 1

    def test_blocked_when_both_sides_lack_entitlement(
        self, db, logged_in_client, user, accounts, auditor, monkeypatch
    ):
        """auditor も owner も `audit_seat` を持たない場合は拒否"""
        _patch_billing(monkeypatch, auditor_ok=False, owner_ok=False)

        resp = logged_in_client.post(
            "/settings/audit/add",
            data={"username": auditor.username, "permission_level": "3"},
        )
        assert resp.status_code in (302, 303)
        assert AuditGrant.query.count() == 0

    def test_allowed_when_only_auditor_has_seat(
        self, db, logged_in_client, user, accounts, auditor, monkeypatch
    ):
        """監査者課金モデル: auditor 側だけで OK"""
        _patch_billing(monkeypatch, auditor_ok=True, owner_ok=False)

        resp = logged_in_client.post(
            "/settings/audit/add",
            data={"username": auditor.username, "permission_level": "3"},
        )
        assert resp.status_code in (302, 303)
        assert AuditGrant.query.count() == 1

    def test_allowed_when_only_owner_has_seat(
        self, db, logged_in_client, user, accounts, auditor, monkeypatch
    ):
        """被監査者課金モデル: owner (current_user) 側だけで OK"""
        _patch_billing(monkeypatch, auditor_ok=False, owner_ok=True)

        resp = logged_in_client.post(
            "/settings/audit/add",
            data={"username": auditor.username, "permission_level": "3"},
        )
        assert resp.status_code in (302, 303)
        assert AuditGrant.query.count() == 1


# --- 代理閲覧時の audit_permission_check -----------------------------------


class TestActingAsAuditorGate:
    """`acting_as_user_id` セッション中の有償ゲート"""

    def test_unlimited_allows_acting(self, db, client, user, accounts, auditor):
        """デフォルト (unlimited) では代理閲覧は通常通り動く"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=3,
        )
        db.session.add(grant)
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 3

        resp = client.get("/")
        # 200 (代理閲覧成功) または 302 (別画面リダイレクト) を許容、
        # entitlement 失効時のリダイレクト先 (/auditor) ではないこと
        assert resp.status_code in (200, 302, 303)
        if resp.status_code in (302, 303):
            assert "/auditor" not in resp.headers.get("Location", "")

    def test_acting_blocked_when_entitlement_lost(
        self, db, client, user, accounts, auditor, monkeypatch
    ):
        """代理閲覧中に両者とも `audit_seat` 失効 → セッションクリア + 監査ダッシュボードへ"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=3,
        )
        db.session.add(grant)
        db.session.commit()

        _patch_billing(monkeypatch, auditor_ok=False, owner_ok=False)

        with client.session_transaction() as sess:
            sess["_user_id"] = str(auditor.id)
            sess["acting_as_user_id"] = user.id
            sess["acting_as_permission_level"] = 3

        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 303)
        # acting_as_user_id がクリアされている (= 代理閲覧が終了)
        with client.session_transaction() as sess:
            assert sess.get("acting_as_user_id") is None
