"""Lv2 監査者向けフロー (journal の編集) のテスト

旧リアルタイム代理閲覧 (acting_as_user_id) の撤去 (#112) 後は、Lv2 の
平文編集エンドポイント自体が撤去・GET 専用化されている。ここでは旧経路が
到達不能 (404/405) であることのみを担保する。
"""

import json
from datetime import date

import pytest

from app.models.audit import AuditGrant, AuditGrantAccount
from app.models.journal import JournalEntry, JournalEntryLine


@pytest.fixture
def lv2_setup(db, client, user, auditor, accounts):
    """Lv2 監査 grant を作り、auditor としてログインした状態にする。

    旧代理閲覧 (acting_as_user_id) は撤去済みのため、auditor は本人として
    ログインするだけ。旧 Lv2 編集経路が到達不能であることの検証に使う。
    """
    grant = AuditGrant(
        owner_user_id=user.id,
        auditor_user_id=auditor.id,
        permission_level=2,
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
    return grant


@pytest.fixture
def mixed_journal(db, user, accounts):
    """公開科目 (5010, 1010) と非公開科目 (5020) を含む仕訳"""
    e = JournalEntry(
        user_id=user.id,
        entry_number=1,

        # E3-F: 実エントリ同様に fiscal_year/fiscal_month を populate
        # (check_entry_modifiable は fiscal_year/fiscal_month を読む)。
        fiscal_year=2026, fiscal_month=2,
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


# 旧 Lv2 masking (非公開行を事業主集約行にまとめる get_json / edit の proprietor
# 集約) は旧リアルタイム代理閲覧の撤去 (#112) に伴い廃止したため、関連テスト
# (TestJournalGetJsonLv2 / TestJournalEditGetLv2) は削除した。


class TestJournalEditApiLv2Removed:
    """旧 Lv2 モーダル編集 (/journal/<id>/edit-api) は撤去済み。

    E2EE では編集が暗号化済み PUT /api/v1/journals/<id> に一本化される。
    旧リアルタイム代理閲覧も撤去済み (#112) で、Lv2 監査者による仕訳編集は
    アーキテクチャ上サポートされない。ここでは旧平文エンドポイントが到達不能
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


# 旧 Discord 通知 (_mark_draft_done / notify.py / ai_drafts.discord_*) は
# E6 PR-3 (#113 §15.3) で完全廃止。旧 auto_import フロー (048 で削除済) 専用の
# 死コードだった。関連テスト TestMarkDraftDone も削除。POST /api/v1/journals に
# draft_id を渡したときの draft→voucher 移行は test_api 側で別途カバーされる。
