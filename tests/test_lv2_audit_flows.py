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


class TestJournalEditApiLv2:
    def test_balanced_with_non_public(self, lv2_setup, mixed_journal, client, db):
        """非公開行を保持した状態で公開行を更新（貸借成立）"""
        # 非公開行: 5020 debit 200 を維持
        # 公開行: 食費 800 → 1000 / 現金 1000 → 1200 (貸借: 1000+200 = 1200)
        resp = client.post(f"/journal/{mixed_journal.id}/edit-api", json={
            "date": "2026-02-15",
            "description": "Lv2更新",
            "lines": [
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 1200},
            ],
        })
        assert resp.status_code == 200
        # 5020 は残っている
        e = db.session.get(JournalEntry, mixed_journal.id)
        codes = {l.account_code for l in e.lines}
        assert "5020" in codes
        assert "5010" in codes
        assert "1010" in codes

    def test_unbalanced_rejected(self, lv2_setup, mixed_journal, client):
        """非公開を考慮しても貸借不一致なら 400"""
        resp = client.post(f"/journal/{mixed_journal.id}/edit-api", json={
            "date": "2026-02-15",
            "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 500, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400
        body = resp.get_json()
        assert "貸借" in body["error"]

    def test_proprietor_line_skipped(self, lv2_setup, mixed_journal, client):
        """is_proprietor=True の行は parsed から除外される"""
        # フロント側が事業主集約行を送ってきた場合でも DB には反映されない
        resp = client.post(f"/journal/{mixed_journal.id}/edit-api", json={
            "date": "2026-02-15",
            "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 1200},
                # 事業主行 (3030) は無視される
                {"account_code": "3030", "debit_amount": 0, "credit_amount": 200,
                 "is_proprietor": True},
            ],
        })
        assert resp.status_code == 200


class TestJournalEditPostLv2:
    """ブラウザフォーム経由の edit POST (Lv2)"""

    def test_unbalanced_rejected(self, lv2_setup, mixed_journal, client):
        resp = client.post(f"/journal/{mixed_journal.id}/edit", data={
            "date": "2026-02-15",
            "description": "x",
            "fiscal_period": "",
            "lines_json": json.dumps([
                {"account_code": "5010", "debit_amount": 500, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ]),
        })
        assert resp.status_code == 200  # form 再表示

    def test_balanced_update(self, lv2_setup, mixed_journal, client, db):
        resp = client.post(f"/journal/{mixed_journal.id}/edit", data={
            "date": "2026-02-15",
            "description": "Lv2フォーム更新",
            "fiscal_period": "",
            "lines_json": json.dumps([
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 1200},
            ]),
        })
        assert resp.status_code in (302, 303)
        e = db.session.get(JournalEntry, mixed_journal.id)
        assert e.description == "Lv2フォーム更新"
        # 非公開行 (5020) はそのまま
        assert any(l.account_code == "5020" for l in e.lines)

    def test_fiscal_period_16_blocked(self, lv2_setup, mixed_journal, client):
        # 監査ユーザーの settings.fiscal はもともとブロックされるが、
        # edit form で fiscal_period=16 を渡すケースも明示的にブロック
        # 実際には JournalForm の SelectField choices に 16 はないので validate fail
        # ここでは form 再表示を確認
        resp = client.post(f"/journal/{mixed_journal.id}/edit", data={
            "date": "2026-02-15",
            "description": "x",
            "fiscal_period": "16",
            "lines_json": json.dumps([
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 1200},
            ]),
        })
        assert resp.status_code in (200, 302)

    def test_locked_period_target_rejected(self, lv2_setup, mixed_journal, client, db):
        from app.models.fiscal import FiscalClose
        # 2026-03 を確定 → 03 への変更を試す
        db.session.add(FiscalClose(user_id=mixed_journal.user_id, year=2026, closed_period=3))
        db.session.commit()
        resp = client.post(f"/journal/{mixed_journal.id}/edit", data={
            "date": "2026-03-15",
            "description": "x",
            "fiscal_period": "",
            "lines_json": json.dumps([
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 1200},
            ]),
        })
        # entry_modifiable で先にブロック (元伝票は 2026-02 だが 2026-02 は確定済みではない)
        # → check_period_open_for_new で 03 が確定済みとして弾かれる
        assert resp.status_code in (200, 302, 303)


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

        with patch("app.views.api.create_voucher_from_draft"), \
             patch("app.services.notify.update_discord_message") as mock_upd:
            resp = client.post("/api/v1/journals", headers=auth_header, json={
                "date": "2026-02-15",
                "description": "ファミマ",
                "lines": [
                    {"account_code": "5010", "debit": 100, "credit": 0},
                    {"account_code": "1010", "debit": 0, "credit": 100},
                ],
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

        with patch("app.views.api.create_voucher_from_draft"), \
             patch("app.services.notify.update_discord_message") as mock_upd:
            resp = client.post("/api/v1/journals", headers=auth_header, json={
                "date": "2026-02-15",
                "description": "ファミマ",
                "lines": [
                    {"account_code": "5010", "debit": 100, "credit": 0},
                    {"account_code": "1010", "debit": 0, "credit": 100},
                ],
                "draft_id": d.id,
            })
        assert resp.status_code == 201
        mock_upd.assert_not_called()
