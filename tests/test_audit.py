"""監査連携の API 認証・ユーザー隔離・AuditGrant モデルのテスト

旧リアルタイム代理閲覧 (#112) に依存していた科目隠蔽 (mask_account_name /
get_allowed_account_codes / is_entry_locked_for_auditor / get_proprietor_account_code)
と owner 編集ロックは撤去済みのため、関連テストは削除した
(サービス関数自体も app/services/audit.py ごと削除)。
"""

import pytest

from app.models.audit import AuditGrant, AuditGrantAccount
from tests.conftest import make_journal


# =================================================================
# API 認証・スコープ・ユーザー隔離
# =================================================================


class TestAPIAuth:
    """REST API の認証・権限テスト"""

    def test_no_auth_header(self, client, db, user, accounts):
        """認証ヘッダーなし → 401"""
        resp = client.get("/api/v1/journals")
        assert resp.status_code == 401

    def test_invalid_key(self, client, db, user, accounts):
        """無効なキー → 401"""
        resp = client.get("/api/v1/journals",
                          headers={"Authorization": "Bearer ik_invalid"})
        assert resp.status_code == 401

    def test_inactive_key(self, client, db, user, accounts):
        """無効化されたキー → 401"""
        from app.models.api_key import APIKey
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="inactive",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:read", is_active=False,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.get("/api/v1/journals",
                          headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 401

    def test_missing_bearer_prefix(self, client, db, user, accounts, api_key_raw):
        """Bearer プレフィックスなし → 401"""
        raw_key, _ = api_key_raw
        resp = client.get("/api/v1/journals",
                          headers={"Authorization": raw_key})
        assert resp.status_code == 401

    def test_read_scope_allows_list(self, client, db, user, accounts):
        """journals:read スコープで一覧取得OK"""
        from app.models.api_key import APIKey
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="readonly",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:read", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.get("/api/v1/journals",
                          headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 200

    def test_read_scope_blocks_create(self, client, db, user, accounts):
        """journals:read スコープで起票 → 403"""
        from app.models.api_key import APIKey
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="readonly",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:read", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.post("/api/v1/journals",
                           headers={"Authorization": f"Bearer {raw_key}"},
                           json={"date": "2026-01-01", "description": "x", "lines": []})
        assert resp.status_code == 403

    def test_read_scope_blocks_delete(self, client, db, user, accounts):
        """journals:read スコープで削除 → 403"""
        from app.models.api_key import APIKey
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="readonly",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:read", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        resp = client.delete(f"/api/v1/journals/{entry.id}",
                             headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 403

    def test_create_scope_blocks_read(self, client, db, user, accounts):
        """journals:create のみでは一覧取得 → 403"""
        from app.models.api_key import APIKey
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="writeonly",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:create", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.get("/api/v1/journals",
                          headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 403

    def test_delete_scope_blocks_create(self, client, db, user, accounts):
        """journals:delete のみでは起票 → 403"""
        from app.models.api_key import APIKey
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user.id, name="deleteonly",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes="journals:delete", is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        resp = client.post("/api/v1/journals",
                           headers={"Authorization": f"Bearer {raw_key}"},
                           json={"date": "2026-01-01", "description": "x", "lines": []})
        assert resp.status_code == 403


class TestAPIUserIsolation:
    """API のユーザー間データ隔離テスト"""

    def _make_key(self, db, user_id, scopes="journals:read,journals:create,journals:delete"):
        from app.models.api_key import APIKey
        raw_key, key_hash, key_prefix = APIKey.generate()
        key = APIKey(
            user_id=user_id, name="test",
            key_hash=key_hash, key_prefix=key_prefix,
            scopes=scopes, is_active=True,
        )
        db.session.add(key)
        db.session.commit()
        return raw_key

    def test_list_isolation(self, client, db, user, accounts, auditor):
        """他ユーザーの仕訳は一覧に表示されない"""
        make_journal(db, user.id, "5010", "1010", 1000)
        key = self._make_key(db, auditor.id, scopes="journals:read")
        resp = client.get("/api/v1/journals",
                          headers={"Authorization": f"Bearer {key}"})
        assert resp.get_json()["total"] == 0

    def test_detail_isolation(self, client, db, user, accounts, auditor):
        """他ユーザーの仕訳は詳細取得で 404"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        key = self._make_key(db, auditor.id, scopes="journals:read")
        resp = client.get(f"/api/v1/journals/{entry.id}",
                          headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 404

    def test_delete_isolation(self, client, db, user, accounts, auditor):
        """他ユーザーの仕訳は削除で 404"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        key = self._make_key(db, auditor.id, scopes="journals:delete")
        resp = client.delete(f"/api/v1/journals/{entry.id}",
                             headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 404

    def test_draft_isolation(self, client, db, user, accounts, auditor):
        """他ユーザーの下書きは一覧に表示されない"""
        key = self._make_key(db, auditor.id, scopes="ai:analyze")
        resp = client.get("/api/v1/ai/drafts",
                          headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200
        assert resp.get_json()["total"] == 0

    def test_draft_detail_isolation(self, client, db, user, accounts, auditor):
        """他ユーザーの下書きは詳細取得で 404"""
        key = self._make_key(db, auditor.id, scopes="ai:analyze")
        resp = client.get("/api/v1/ai/drafts/1",
                          headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 404


# =================================================================
# AuditGrant モデル
# =================================================================


class TestAuditGrantModel:
    def test_unique_constraint(self, db, user, auditor):
        """同一オーナー・監査者のグラントは1つのみ"""
        g1 = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=1,
        )
        db.session.add(g1)
        db.session.commit()

        g2 = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2,
        )
        db.session.add(g2)
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()

    def test_cascade_delete_accounts(self, db, user, accounts, auditor):
        """グラント削除時に公開科目も削除される"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2,
        )
        db.session.add(grant)
        db.session.commit()
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id, account_user_id=user.id, account_code="5010",
        ))
        db.session.commit()
        grant_id = grant.id
        db.session.delete(grant)
        db.session.commit()
        assert AuditGrantAccount.query.filter_by(audit_grant_id=grant_id).count() == 0

    def test_permission_levels(self, db, user, auditor):
        """Lv1/2/3 が正しく保存される"""
        for level in [1, 2, 3]:
            db.session.rollback()
            # クリーンアップ
            AuditGrant.query.filter_by(
                owner_user_id=user.id, auditor_user_id=auditor.id
            ).delete()
            db.session.commit()

            grant = AuditGrant(
                owner_user_id=user.id, auditor_user_id=auditor.id,
                permission_level=level,
            )
            db.session.add(grant)
            db.session.commit()
            assert grant.permission_level == level
