"""Lv2 監査者向けフロー (journal の編集) のテスト

acting_as_user_id + permission_level=2 + AuditGrantAccount で
公開科目のみ編集可能、非公開行は保持の挙動を網羅。
"""

import json
from datetime import date

import pytest

from app.models.audit import AuditGrant, AuditGrantAccount
from app.models.journal import JournalEntry, JournalEntryLine


@pytest.fixture
def lv2_setup(db, client, user, auditor, accounts):
    """Lv2 監査者として user の代理閲覧を行う状態を作る"""
    grant = AuditGrant(
        owner_user_id=user.id,
        auditor_user_id=auditor.id,
        permission_level=2,
        status="active",
    )
    db.session.add(grant)
    db.session.flush()
    # 5010 (食費) と 1010 (現金) を公開
    db.session.add_all([
        AuditGrantAccount(audit_grant_id=grant.id,
                          account_user_id=user.id, account_code="5010"),
        AuditGrantAccount(audit_grant_id=grant.id,
                          account_user_id=user.id, account_code="1010"),
        AuditGrantAccount(audit_grant_id=grant.id,
                          account_user_id=user.id, account_code="3030"),  # 事業主
    ])
    db.session.commit()

    with client.session_transaction() as sess:
        sess["_user_id"] = str(auditor.id)
        sess["acting_as_user_id"] = user.id
        sess["acting_as_permission_level"] = 2
    return grant


@pytest.fixture
def mixed_journal(db, user, accounts):
    """公開科目 (5010, 1010) と非公開科目 (5020) を含む仕訳"""
    e = JournalEntry(
        user_id=user.id, date=date(2026, 2, 15),
        entry_number=1, description="複合仕訳",
        source="journal",
    )
    e.lines = [
        # 公開: 食費 800 (debit) + 住居費 200 (debit) = 1000
        JournalEntryLine(account_user_id=user.id, account_code="5010",
                         debit_amount=800, credit_amount=0),
        JournalEntryLine(account_user_id=user.id, account_code="5020",
                         debit_amount=200, credit_amount=0),  # 非公開
        # 公開: 現金 1000 (credit)
        JournalEntryLine(account_user_id=user.id, account_code="1010",
                         debit_amount=0, credit_amount=1000),
    ]
    db.session.add(e)
    db.session.commit()
    return e


class TestJournalGetJsonLv2:
    def test_aggregates_non_public_into_proprietor(self, lv2_setup, mixed_journal, client):
        resp = client.get(f"/journal/{mixed_journal.id}/json")
        assert resp.status_code == 200
        data = resp.get_json()
        # 公開行 (5010, 1010) と事業主集約行 (3030) が含まれる
        codes = [l["account_code"] for l in data["lines"]]
        assert "5010" in codes
        assert "1010" in codes
        # 5020 (非公開) は事業主に集約される
        assert "5020" not in codes
        assert "3030" in codes  # proprietor


class TestJournalEditGetLv2:
    """Lv2 監査者が編集画面 (GET /journal/<id>/edit) を開いたとき、
    非公開行を事業主集約行 (is_proprietor) にまとめた existing_lines が
    フォームに渡る (保存自体は暗号化 PUT で行うため別経路)。"""

    def test_edit_form_aggregates_non_public_into_proprietor(
        self, lv2_setup, mixed_journal, client
    ):
        resp = client.get(f"/journal/{mixed_journal.id}/edit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # existing_lines は JSON で埋め込まれる。公開行 + proprietor 集約行を含む。
        assert '"is_proprietor": true' in body or '"is_proprietor":true' in body
        assert "5010" in body
        # 非公開コード 5020 は集約され生の科目コードとしては出ない
        assert '"account_code": "5020"' not in body


class TestJournalEditApiLv2Removed:
    """旧 Lv2 モーダル編集 (/journal/<id>/edit-api) は撤去済み。

    E2EE では編集が暗号化済み PUT /api/v1/journals/<id> に一本化される。
    代理閲覧中の監査者は owner の MK を持たず暗号値を復号・再暗号化できないため、
    PUT/batch 側の代理ガードでブロックされる (Lv2 監査者による仕訳編集は
    アーキテクチャ上サポートされない)。ここでは旧平文エンドポイントが到達不能
    (404/405) であることのみ担保する。
    """

    def test_edit_api_removed(self, lv2_setup, mixed_journal, client):
        resp = client.post(f"/journal/{mixed_journal.id}/edit-api", json={
            "date": "2026-02-15",
            "description": "Lv2更新",
            "lines": [
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 1200},
            ],
        })
        # ルート自体が存在しないため POST は受け付けない (404 / 405)
        assert resp.status_code in (404, 405)


class TestJournalEditPostLv2Removed:
    """E3-F PR-B2 で /journal/<id>/edit は GET 専用化された。

    Lv2 の編集経路はモーダル経由の /journal/<id>/edit-api (TestJournalEditApiLv2)
    が引き続き本流。form POST が 405 を返すことを担保する。"""

    def test_post_returns_405(self, lv2_setup, mixed_journal, client):
        resp = client.post(f"/journal/{mixed_journal.id}/edit", data={
            "date": "2026-02-15",
            "description": "x",
            "fiscal_period": "",
            "lines_json": json.dumps([
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 1200},
            ]),
        })
        assert resp.status_code == 405


# _send_draft_notification は POST /api/v1/ai/analyze 削除に伴い廃止済。
# Webhook 通知は将来 PATCH /api/v1/ai/drafts/<id>/suggestions 側に統合される
# (現状一時的に Bearer API 経由の AI 下書き作成通知は無効)。


class TestMarkDraftDone:
    """API: POST /api/v1/journals with draft_id で _mark_draft_done 経由"""

    def test_with_discord_message_id(self, db, user, auth_header, accounts, client):
        from app.models.ai_draft import AIDraft
        from unittest.mock import patch
        import json as json_lib

        d = AIDraft(
            user_id=user.id,
            image_key="drafts/1/test.png", image_mime="image/png",
            file_hash="h",
            suggestions_json=json_lib.dumps([{
                "title": "t", "date": "2026-02-15",
                "entry_description": "ファミマ",
                "lines": [
                    {"account_code": "5010", "debit_amount": 100,
                     "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0,
                     "credit_amount": 100},
                ],
            }]),
            status="analyzed",
            discord_webhook_url="https://discord.com/api/webhooks/x/y",
            discord_message_id="msg-1",
        )
        db.session.add(d)
        db.session.commit()

        from tests.conftest import encrypt_lines, encrypted_payload
        with patch("app.views.api.create_voucher_from_draft"), \
             patch("app.services.notify.update_discord_message") as mock_upd:
            resp = client.post("/api/v1/journals", headers=auth_header, json={
                "date": "2026-02-15",
                "description": "ファミマ",
                "lines": encrypt_lines([
                    {"account_code": "5010", "debit": 100, "credit": 0},
                    {"account_code": "1010", "debit": 0, "credit": 100},
                ]),
                **encrypted_payload(),
                "draft_id": d.id,
            })
        assert resp.status_code == 201
        # discord_message_id があるので update_discord_message が呼ばれる
        mock_upd.assert_called_once()

    def test_without_discord_id_skips(self, db, user, auth_header, accounts, client):
        from app.models.ai_draft import AIDraft
        from unittest.mock import patch
        import json as json_lib

        d = AIDraft(
            user_id=user.id,
            image_key="drafts/1/test.png", image_mime="image/png",
            file_hash="h",
            suggestions_json=json_lib.dumps([{
                "title": "t", "date": "2026-02-15",
                "entry_description": "x", "lines": [],
            }]),
            status="analyzed",
            # discord_message_id なし
        )
        db.session.add(d)
        db.session.commit()

        from tests.conftest import encrypt_lines, encrypted_payload
        with patch("app.views.api.create_voucher_from_draft"), \
             patch("app.services.notify.update_discord_message") as mock_upd:
            resp = client.post("/api/v1/journals", headers=auth_header, json={
                "date": "2026-02-15",
                "description": "ファミマ",
                "lines": encrypt_lines([
                    {"account_code": "5010", "debit": 100, "credit": 0},
                    {"account_code": "1010", "debit": 0, "credit": 100},
                ]),
                **encrypted_payload(),
                "draft_id": d.id,
            })
        assert resp.status_code == 201
        mock_upd.assert_not_called()
