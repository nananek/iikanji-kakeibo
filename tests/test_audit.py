"""audit.py のテスト — 監査権限・提出ロック・科目隠蔽・ユーザー隔離"""

from datetime import date

import pytest

from app.models.account import Account
from app.models.audit import AuditGrant, AuditGrantAccount
from app.models.journal import JournalEntry
from app.services.audit import (
    get_allowed_account_codes,
    get_proprietor_account_code,
    get_submitted_account_codes,
    is_entry_locked_for_auditor,
    is_entry_locked_for_owner,
    mask_account_name,
)
from tests.conftest import make_journal


# =================================================================
# mask_account_name — 科目名マスキング
# =================================================================


class TestMaskAccountName:
    def test_no_restriction(self):
        """allowed_account_codes が None なら科目名そのまま"""
        assert mask_account_name("食費", "5010", None) == "食費"

    def test_allowed_account(self):
        """公開科目は科目名そのまま"""
        assert mask_account_name("食費", "5010", {"5010", "1010", "1020"}) == "食費"

    def test_hidden_account(self):
        """非公開科目は「事業主」に差し替え"""
        assert mask_account_name("食費", "5010", {"1010", "1020", "2010"}) == "事業主"

    def test_empty_allowed_set(self):
        """公開科目が空の場合は全て「事業主」"""
        assert mask_account_name("現金", "1010", set()) == "事業主"


# =================================================================
# get_proprietor_account_code — 事業主科目コード取得
# =================================================================


class TestGetProprietorAccountCode:
    def test_found(self, db, user, accounts):
        """事業主科目がある場合"""
        result = get_proprietor_account_code(user.id)
        assert result == accounts["3030"].code

    def test_not_found(self, db, user, account_types):
        """事業主科目がない場合"""
        assert get_proprietor_account_code(user.id) is None


# =================================================================
# get_submitted_account_codes — 提出済み公開科目
# =================================================================


class TestGetSubmittedAccountCodes:
    def test_no_grants(self, db, user, accounts):
        """グラントなし → 空"""
        result = get_submitted_account_codes(user.id)
        assert result == set()

    def test_draft_grant_not_included(self, db, user, accounts, auditor):
        """下書き状態のグラントは含まれない"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="draft",
        )
        db.session.add(grant)
        db.session.commit()
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id,
            account_user_id=user.id,
            account_code="5010",
        ))
        db.session.commit()
        result = get_submitted_account_codes(user.id)
        assert result == set()

    def test_submitted_grant(self, db, user, accounts, auditor):
        """提出済みグラントの公開科目が返される"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="submitted",
        )
        db.session.add(grant)
        db.session.commit()
        db.session.add_all([
            AuditGrantAccount(audit_grant_id=grant.id, account_user_id=user.id, account_code="5010"),
            AuditGrantAccount(audit_grant_id=grant.id, account_user_id=user.id, account_code="5020"),
        ])
        db.session.commit()
        result = get_submitted_account_codes(user.id)
        assert result == {"5010", "5020"}

    def test_lv1_grant_not_included(self, db, user, accounts, auditor):
        """Lv1グラント（提出済み）は対象外（Lv2のみ）"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=1, status="submitted",
        )
        db.session.add(grant)
        db.session.commit()
        result = get_submitted_account_codes(user.id)
        assert result == set()

    def test_lv3_grant_not_included(self, db, user, accounts, auditor):
        """Lv3グラント（提出済み）は対象外（Lv2のみ）"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=3, status="submitted",
        )
        db.session.add(grant)
        db.session.commit()
        result = get_submitted_account_codes(user.id)
        assert result == set()

    def test_other_user_grants_not_included(self, db, user, accounts, auditor):
        """他ユーザーのグラントは含まれない"""
        grant = AuditGrant(
            owner_user_id=auditor.id, auditor_user_id=user.id,
            permission_level=2, status="submitted",
        )
        db.session.add(grant)
        db.session.commit()
        result = get_submitted_account_codes(user.id)
        assert result == set()


# =================================================================
# is_entry_locked_for_owner — 伝票の提出ロック
# =================================================================


class TestIsEntryLockedForOwner:
    def test_no_grant(self, db, user, accounts):
        """グラントなし → ロックなし"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        assert is_entry_locked_for_owner(user.id, entry) is False

    def test_not_locked_when_no_overlap(self, db, user, accounts, auditor):
        """提出済み科目と伝票の科目が重ならない → ロックなし"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="submitted",
        )
        db.session.add(grant)
        db.session.commit()
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id, account_user_id=user.id, account_code="5020",
        ))
        db.session.commit()
        # 5010(食費)/1010(現金) → 5020 はロック対象だが使っていない
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        assert is_entry_locked_for_owner(user.id, entry) is False

    def test_locked_when_overlap(self, db, user, accounts, auditor):
        """提出済み科目を含む伝票 → ロック"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="submitted",
        )
        db.session.add(grant)
        db.session.commit()
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id, account_user_id=user.id, account_code="5010",
        ))
        db.session.commit()
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        assert is_entry_locked_for_owner(user.id, entry) is True

    def test_locked_by_credit_side(self, db, user, accounts, auditor):
        """貸方科目がロック対象でもロックされる"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="submitted",
        )
        db.session.add(grant)
        db.session.commit()
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id, account_user_id=user.id, account_code="1010",
        ))
        db.session.commit()
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        assert is_entry_locked_for_owner(user.id, entry) is True

    def test_draft_grant_does_not_lock(self, db, user, accounts, auditor):
        """下書き状態のグラントではロックされない"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="draft",
        )
        db.session.add(grant)
        db.session.commit()
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id, account_user_id=user.id, account_code="5010",
        ))
        db.session.commit()
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        assert is_entry_locked_for_owner(user.id, entry) is False


# =================================================================
# is_entry_locked_for_auditor — 監査者の編集制限
# =================================================================


class TestIsEntryLockedForAuditor:
    def test_no_restriction(self, db, user, accounts):
        """allowed_account_codes=None（Lv3等） → ロックなし"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        assert is_entry_locked_for_auditor(entry, None) is False

    def test_all_accounts_allowed(self, db, user, accounts):
        """全科目が公開 → ロックなし"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        allowed = {"5010", "1010"}
        assert is_entry_locked_for_auditor(entry, allowed) is False

    def test_partial_hidden(self, db, user, accounts):
        """一部が非公開 → ロック"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        allowed = {"5010"}  # 1010 は非公開
        assert is_entry_locked_for_auditor(entry, allowed) is True

    def test_all_hidden(self, db, user, accounts):
        """全科目が非公開 → ロック"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        assert is_entry_locked_for_auditor(entry, set()) is True


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


class TestAPISubmitLock:
    """API における提出ロック（Lv2グラント提出済み科目の制限）"""

    def _setup_submitted_grant(self, db, user, accounts, auditor, locked_account_codes):
        """提出済みLv2グラントを作成"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="submitted",
        )
        db.session.add(grant)
        db.session.commit()
        for code in locked_account_codes:
            db.session.add(AuditGrantAccount(
                audit_grant_id=grant.id, account_user_id=user.id, account_code=code,
            ))
        db.session.commit()
        return grant

    def test_create_blocked_by_submit_lock(self, client, db, user, accounts,
                                            auditor, auth_header):
        """提出済み科目を含む仕訳の起票が拒否される"""
        from tests.conftest import encrypt_lines, encrypted_payload
        self._setup_submitted_grant(db, user, accounts, auditor,
                                     ["5010"])
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "ロック対象",
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 1000},
                {"account_code": accounts["1010"].code, "credit": 1000},
            ]),
            **encrypted_payload(),
        })
        assert resp.status_code == 400
        assert "提出済み" in resp.get_json()["error"]

    def test_create_allowed_for_unlocked_accounts(self, client, db, user, accounts,
                                                    auditor, auth_header):
        """提出済み科目を含まない仕訳は起票できる"""
        from tests.conftest import encrypt_lines, encrypted_payload
        self._setup_submitted_grant(db, user, accounts, auditor,
                                     ["5020"])
        resp = client.post("/api/v1/journals", headers=auth_header, json={
            "date": "2026-02-15",
            "description": "ロック対象外",
            "lines": encrypt_lines([
                {"account_code": accounts["5010"].code, "debit": 1000},
                {"account_code": accounts["1010"].code, "credit": 1000},
            ]),
            **encrypted_payload(),
        })
        assert resp.status_code == 201

    def test_delete_blocked_by_submit_lock(self, client, db, user, accounts,
                                            auditor, auth_header):
        """提出済み科目を含む伝票の削除が拒否される"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        self._setup_submitted_grant(db, user, accounts, auditor,
                                     ["5010"])
        resp = client.delete(f"/api/v1/journals/{entry.id}", headers=auth_header)
        assert resp.status_code == 400
        assert "提出済み" in resp.get_json()["error"]

    def test_delete_allowed_for_unlocked_entry(self, client, db, user, accounts,
                                                auditor, auth_header):
        """提出済み科目を含まない伝票は削除できる"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        self._setup_submitted_grant(db, user, accounts, auditor,
                                     ["5020"])
        resp = client.delete(f"/api/v1/journals/{entry.id}", headers=auth_header)
        assert resp.status_code == 200


# =================================================================
# AuditGrant モデル
# =================================================================


class TestAuditGrantModel:
    def test_unique_constraint(self, db, user, auditor):
        """同一オーナー・監査者のグラントは1つのみ"""
        g1 = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=1, status="draft",
        )
        db.session.add(g1)
        db.session.commit()

        g2 = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="draft",
        )
        db.session.add(g2)
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()

    def test_cascade_delete_accounts(self, db, user, accounts, auditor):
        """グラント削除時に公開科目も削除される"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="draft",
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
                permission_level=level, status="draft",
            )
            db.session.add(grant)
            db.session.commit()
            assert grant.permission_level == level

    def test_status_transitions(self, db, user, auditor):
        """ステータスの遷移: draft → submitted"""
        grant = AuditGrant(
            owner_user_id=user.id, auditor_user_id=auditor.id,
            permission_level=2, status="draft",
        )
        db.session.add(grant)
        db.session.commit()
        assert grant.status == "draft"

        grant.status = "submitted"
        db.session.commit()
        refreshed = db.session.get(AuditGrant, grant.id)
        assert refreshed.status == "submitted"
