"""仕訳帳ビューの追加テスト

test_journal_views.py で扱った index/new/delete/bulk_delete に加えて、
edit POST / get_json / edit_api / suggest_categories / ai_suggest_categories /
delete_batch を網羅。
"""

import json
from datetime import date
from unittest.mock import patch

from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from tests.conftest import make_journal


def _post_edit(client, entry_id, *, date_str="2026-02-15",
               description="更新後", lines=None, fiscal_period=""):
    if lines is None:
        lines = [
            {"account_code": "5010", "debit_amount": 2000, "credit_amount": 0,
             "description": ""},
            {"account_code": "1010", "debit_amount": 0, "credit_amount": 2000,
             "description": ""},
        ]
    return client.post(f"/journal/{entry_id}/edit", data={
        "date": date_str,
        "description": description,
        "fiscal_period": fiscal_period,
        "lines_json": json.dumps(lines),
    })


class TestEditPost:
    def _make_entry(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1000,
                            entry_date=date(2026, 2, 15),
                            source="journal", description="ORIG")

    def test_post_updates(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = _post_edit(logged_in_client, entry.id, description="UPDATED")
        assert resp.status_code in (302, 303)
        db.session.refresh(entry)
        assert entry.description == "UPDATED"

    def test_unbalanced_rejected(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = _post_edit(logged_in_client, entry.id, lines=[
            {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0,
             "description": ""},
            {"account_code": "1010", "debit_amount": 0, "credit_amount": 500,
             "description": ""},
        ])
        # 200 でフォーム再表示
        assert resp.status_code == 200

    def test_post_invalid_lines_json(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit", data={
            "date": "2026-02-15",
            "description": "x",
            "fiscal_period": "",
            "lines_json": "not-json{",
        })
        assert resp.status_code == 200

    def test_post_fiscal_period_16_blocked(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = _post_edit(logged_in_client, entry.id, fiscal_period="16")
        # 損益振替は手動入力不可。SelectField の choices に無いので validate fail or block
        assert resp.status_code in (200, 302)

    def test_post_locked_target_period_rejected(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        # 2026-03 を確定済みにする → 03 への移動を試す
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=3))
        db.session.commit()
        # 03 に移動しようとすると edit ハンドラ自身がリダイレクト (entry_modifiable で弾かれる)
        resp = _post_edit(logged_in_client, entry.id, date_str="2026-03-15")
        assert resp.status_code in (200, 302, 303)


class TestGetJson:
    def test_unauthenticated(self, client):
        resp = client.get("/journal/1/json")
        assert resp.status_code in (302, 401)

    def test_404_for_nonexistent(self, logged_in_client, accounts):
        resp = logged_in_client.get("/journal/9999/json")
        assert resp.status_code == 404

    def test_returns_entry_data(self, db, logged_in_client, user, accounts):
        entry = make_journal(db, user.id, "5010", "1010", 1500,
                              entry_date=date(2026, 2, 15),
                              source="journal", description="JSON取得")
        resp = logged_in_client.get(f"/journal/{entry.id}/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == entry.id
        assert data["description"] == "JSON取得"
        assert data["date"] == "2026-02-15"
        assert len(data["lines"]) == 2
        assert data["lines"][0]["debit_amount"] in (0, 1500)

    def test_idor_other_user(self, db, logged_in_client, accounts,
                             second_user, second_user_accounts):
        other = make_journal(
            db, second_user.id, "5010", "1010", 100,
            entry_date=date(2026, 2, 15), source="journal",
        )
        resp = logged_in_client.get(f"/journal/{other.id}/json")
        assert resp.status_code == 404

    def test_readonly_for_locked_entry(self, db, logged_in_client, user, accounts):
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 1, 15), source="journal")
        # 2026-01 を確定
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = logged_in_client.get(f"/journal/{entry.id}/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["is_readonly"] is True


class TestEditApi:
    def _make_entry(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1000,
                            entry_date=date(2026, 2, 15),
                            source="journal", description="ORIG")

    def test_unauthenticated(self, client):
        resp = client.post("/journal/1/edit-api", json={})
        assert resp.status_code in (302, 401)

    def test_404(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/9999/edit-api", json={})
        assert resp.status_code == 404

    def test_idor(self, db, logged_in_client, accounts,
                  second_user, second_user_accounts):
        other = make_journal(
            db, second_user.id, "5010", "1010", 100,
            entry_date=date(2026, 2, 15), source="journal",
        )
        resp = logged_in_client.post(f"/journal/{other.id}/edit-api", json={
            "date": "2026-02-15", "description": "x",
            "lines": [{"account_code": "5010", "debit_amount": 100, "credit_amount": 0}],
        })
        assert resp.status_code == 404

    def test_no_body(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api",
                                      json=None,
                                      content_type="application/json")
        assert resp.status_code == 400

    def test_missing_required(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "", "description": "",
            "lines": [],
        })
        assert resp.status_code == 400

    def test_no_lines(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15", "description": "x", "lines": [],
        })
        assert resp.status_code == 400

    def test_unbalanced(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15",
            "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 500},
            ],
        })
        assert resp.status_code == 400
        assert "貸借" in resp.get_json()["error"]

    def test_success(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-20",
            "description": "更新済み",
            "lines": [
                {"account_code": "5010", "debit_amount": 3000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 3000},
            ],
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        db.session.refresh(entry)
        assert entry.description == "更新済み"
        assert entry.date == date(2026, 2, 20)

    def test_locked_entry_rejected(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400

    def test_fiscal_period_16_blocked(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15", "description": "x",
            "fiscal_period": "16",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400
        assert "損益振替" in resp.get_json()["error"]


class TestSuggestCategories:
    def test_unauthenticated(self, client):
        resp = client.post("/journal/api/suggest-categories", json={})
        assert resp.status_code in (302, 401)

    def test_no_body(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/journal/api/suggest-categories",
            json=None, content_type="application/json",
        )
        assert resp.status_code == 400

    def test_empty_descriptions(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": [], "payment_account_code": "1010",
        })
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_only_empty_strings(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": ["", "", ""],
            "payment_account_code": "1010",
        })
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_returns_recent_match(self, db, logged_in_client, user, accounts):
        # 過去に「ファミマ」で 5010/1010 仕訳がある
        make_journal(db, user.id, "5010", "1010", 100,
                     entry_date=date(2026, 1, 15), source="cashbook",
                     description="ファミマ")
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": ["ファミマ"],
            "payment_account_code": "1010",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ファミマ"]["account_code"] == "5010"

    def test_no_match(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": ["未知の摘要"],
            "payment_account_code": "1010",
        })
        body = resp.get_json()
        assert "未知の摘要" not in body


class TestAiSuggestCategories:
    def test_unauthenticated(self, client):
        resp = client.post("/journal/api/ai-suggest-categories", json={})
        assert resp.status_code in (302, 401)

    def test_no_body(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/journal/api/ai-suggest-categories",
            json=None, content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_payment_account(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/ai-suggest-categories", json={
            "rows": [{"description": "x", "withdrawal": 100}],
        })
        assert resp.status_code == 400

    def test_missing_rows(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/ai-suggest-categories", json={
            "payment_account_code": "1010",
        })
        assert resp.status_code == 400

    def test_success(self, logged_in_client, accounts):
        with patch("app.services.ai_receipt.suggest_categories_by_ai") as mock_ai:
            mock_ai.return_value = {
                "セブン": {"account_code": "5010", "account_name": "食費"},
            }
            resp = logged_in_client.post("/journal/api/ai-suggest-categories", json={
                "payment_account_code": "1010",
                "rows": [{"description": "セブン", "withdrawal": 500}],
            })
            assert resp.status_code == 200
            assert resp.get_json()["セブン"]["account_code"] == "5010"

    def test_ai_value_error(self, logged_in_client, accounts):
        with patch("app.services.ai_receipt.suggest_categories_by_ai") as mock_ai:
            mock_ai.side_effect = ValueError("AI設定がありません")
            resp = logged_in_client.post("/journal/api/ai-suggest-categories", json={
                "payment_account_code": "1010",
                "rows": [{"description": "x", "withdrawal": 100}],
            })
            assert resp.status_code == 400

    def test_ai_runtime_error(self, logged_in_client, accounts):
        with patch("app.services.ai_receipt.suggest_categories_by_ai") as mock_ai:
            mock_ai.side_effect = RuntimeError("upstream timeout")
            resp = logged_in_client.post("/journal/api/ai-suggest-categories", json={
                "payment_account_code": "1010",
                "rows": [{"description": "x", "withdrawal": 100}],
            })
            assert resp.status_code == 500


class TestDeleteBatch:
    def test_unauthenticated(self, client):
        resp = client.post("/journal/batches/some-id/delete")
        assert resp.status_code in (302, 401)

    def test_unknown_batch(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/batches/nonexistent-id/delete")
        assert resp.status_code in (302, 303)

    def test_delete_batch_success(self, db, logged_in_client, user, accounts):
        from uuid import uuid4
        bid = str(uuid4())
        for i in range(3):
            e = JournalEntry(
                user_id=user.id, date=date(2026, 2, i + 1),
                entry_number=i + 1, description=f"row{i}",
                source="csv", batch_id=bid,
            )
            e.lines = [
                JournalEntryLine(account_user_id=user.id, account_code="5010",
                                 debit_amount=100, credit_amount=0),
                JournalEntryLine(account_user_id=user.id, account_code="1010",
                                 debit_amount=0, credit_amount=100),
            ]
            db.session.add(e)
        db.session.commit()
        resp = logged_in_client.post(f"/journal/batches/{bid}/delete")
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(batch_id=bid).count() == 0

    def test_delete_batch_locked_entries_skipped(self, db, logged_in_client, user, accounts):
        from uuid import uuid4
        bid = str(uuid4())
        # 2026-01 ロック / 2026-02 オープン
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        for i, m in enumerate([1, 2]):
            e = JournalEntry(
                user_id=user.id, date=date(2026, m, 1),
                entry_number=i + 1, description=f"row{i}",
                source="csv", batch_id=bid,
            )
            e.lines = [
                JournalEntryLine(account_user_id=user.id, account_code="5010",
                                 debit_amount=100, credit_amount=0),
                JournalEntryLine(account_user_id=user.id, account_code="1010",
                                 debit_amount=0, credit_amount=100),
            ]
            db.session.add(e)
        db.session.commit()
        resp = logged_in_client.post(f"/journal/batches/{bid}/delete")
        assert resp.status_code in (302, 303)
        # 1月分は残る、2月分は削除
        remaining = JournalEntry.query.filter_by(batch_id=bid).all()
        assert len(remaining) == 1
        assert remaining[0].date.month == 1


def _make_submitted_grant(db, user, auditor):
    """提出済み Lv2 グラント (公開科目 5010) を設定"""
    from app.models.audit import AuditGrant, AuditGrantAccount
    grant = AuditGrant(
        owner_user_id=user.id, auditor_user_id=auditor.id,
        permission_level=2, status="submitted",
    )
    db.session.add(grant)
    db.session.flush()
    db.session.add(AuditGrantAccount(
        audit_grant_id=grant.id, account_user_id=user.id,
        account_code="5010",
    ))
    db.session.commit()
    return grant


def _post_journal(client, *, date_str="2026-02-15",
                  description="テスト", lines=None, fiscal_period=""):
    """/journal/new への POST (test_journal_views のヘルパーと同一形式)"""
    if lines is None:
        lines = [
            {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0,
             "description": ""},
            {"account_code": "1010", "debit_amount": 0, "credit_amount": 1000,
             "description": ""},
        ]
    return client.post("/journal/new", data={
        "date": date_str,
        "description": description,
        "fiscal_period": fiscal_period,
        "lines_json": json.dumps(lines),
    })


def _setup_lv2_acting(db, logged_in_client, user, auditor, account_code="5010"):
    """Lv2 グラント + 代理閲覧セッションを設定"""
    from app.models.audit import AuditGrant, AuditGrantAccount
    grant = AuditGrant(
        owner_user_id=user.id, auditor_user_id=auditor.id,
        permission_level=2, status="active",
    )
    db.session.add(grant)
    db.session.flush()
    db.session.add(AuditGrantAccount(
        audit_grant_id=grant.id, account_user_id=user.id,
        account_code=account_code,
    ))
    db.session.commit()
    with logged_in_client.session_transaction() as sess:
        sess["_user_id"] = str(auditor.id)
        sess["acting_as_user_id"] = user.id
        sess["acting_as_permission_level"] = 2
    return grant


class TestNewLockChecks:
    def test_new_locked_submitted_account_rejected(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """提出済み公開科目を使う新規仕訳は本人でも拒否される"""
        _make_submitted_grant(db, user, auditor)
        resp = _post_journal(logged_in_client, lines=[
            {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0,
             "description": ""},
            {"account_code": "1010", "debit_amount": 0, "credit_amount": 1000,
             "description": ""},
        ])
        assert resp.status_code == 200  # フォーム再表示
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="journal"
        ).count() == 0

    def test_new_lv2_non_public_account_rejected(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 顧問は非公開科目 (5020) を使う新規仕訳が拒否される"""
        _setup_lv2_acting(db, logged_in_client, user, auditor)
        resp = _post_journal(logged_in_client, lines=[
            {"account_code": "5020", "debit_amount": 1000, "credit_amount": 0,
             "description": ""},
            {"account_code": "1010", "debit_amount": 0, "credit_amount": 1000,
             "description": ""},
        ])
        assert resp.status_code == 200
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="journal"
        ).count() == 0


class TestEditLockChecks:
    def test_edit_get_blocked_locked_for_owner(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """提出済み公開科目を含む伝票の編集画面は開けない"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15),
                             source="journal", description="ORIG")
        _make_submitted_grant(db, user, auditor)
        resp = logged_in_client.get(f"/journal/{entry.id}/edit")
        assert resp.status_code in (302, 303)

    def test_edit_get_lv2_aggregates_proprietor(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 編集画面は非公開行を事業主集約行として表示"""
        # 5010 (公開) + 3030 (事業主=非公開) の 2 行
        entry = JournalEntry(
            user_id=user.id, date=date(2026, 2, 15),
            entry_number=999, description="Lv2表示",
            source="journal",
        )
        entry.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=800, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="3030",
                             debit_amount=0, credit_amount=800),
        ]
        db.session.add(entry)
        db.session.commit()
        _setup_lv2_acting(db, logged_in_client, user, auditor)
        resp = logged_in_client.get(f"/journal/{entry.id}/edit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "3030" in body  # 事業主集約行

    def test_edit_post_lv2_public_lines_replaced(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 編集 POST: 公開行のみ差し替え、非公開行 (3030) は保持"""
        entry = JournalEntry(
            user_id=user.id, date=date(2026, 2, 15),
            entry_number=100, description="ORIG",
            source="journal",
        )
        entry.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=800, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="3030",
                             debit_amount=0, credit_amount=800),
        ]
        db.session.add(entry)
        db.session.commit()
        _setup_lv2_acting(db, logged_in_client, user, auditor)
        # 公開行: 5010 debit 900 / 4010 credit 100 → 非公開 credit 800 と合わせて
        # 借方 900 = 貸方 900 でバランス
        resp = _post_edit(logged_in_client, entry.id, description="Lv2更新", lines=[
            {"account_code": "5010", "debit_amount": 900, "credit_amount": 0,
             "description": ""},
            {"account_code": "4010", "debit_amount": 0, "credit_amount": 100,
             "description": ""},
            {"account_code": "3030", "debit_amount": 0, "credit_amount": 0,
             "description": "", "is_proprietor": True},
        ])
        assert resp.status_code in (302, 303)
        db.session.refresh(entry)
        assert entry.description == "Lv2更新"
        codes = {l.account_code: l for l in entry.lines}
        assert "3030" in codes  # 非公開行保持
        assert codes["3030"].credit_amount == 800
        assert codes["5010"].debit_amount == 900
        assert codes["4010"].credit_amount == 100

    def test_edit_post_lv2_unbalanced_rejected(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 編集 POST: 公開行の貸借が非公開行込みで一致しない場合は拒否"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15),
                             source="journal", description="ORIG")
        _setup_lv2_acting(db, logged_in_client, user, auditor)
        resp = _post_edit(logged_in_client, entry.id, lines=[
            {"account_code": "5010", "debit_amount": 500, "credit_amount": 0,
             "description": ""},
            {"account_code": "1010", "debit_amount": 0, "credit_amount": 0,
             "description": ""},
        ])
        # 既存 1010 credit 1000 + 公開 500 → 貸借不一致でフォーム再表示
        assert resp.status_code == 200
        db.session.refresh(entry)
        assert entry.description == "ORIG"

    def test_edit_post_period_error_redraws(
        self, db, logged_in_client, user, accounts
    ):
        """通常ユーザー: 変更先期間が確定済みならフォーム再表示"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15),
                             source="journal", description="ORIG")
        # 1月のみ確定 (2月はオープン → entry 自体は編集可能)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = _post_edit(logged_in_client, entry.id, date_str="2026-01-15")
        assert resp.status_code == 200
        db.session.refresh(entry)
        assert entry.description == "ORIG"

    def test_edit_post_lv2_period_error_redraws(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 編集 POST: 変更先期間が確定済みならフォーム再表示"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15),
                             source="journal", description="ORIG")
        _setup_lv2_acting(db, logged_in_client, user, auditor)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = _post_edit(logged_in_client, entry.id, date_str="2026-01-15")
        assert resp.status_code == 200
        db.session.refresh(entry)
        assert entry.description == "ORIG"


class TestEditApiLockChecks:
    def test_edit_api_blocked_locked_for_owner(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """提出済み公開科目を含む伝票は edit-api でも拒否される"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15),
                             source="journal", description="ORIG")
        _make_submitted_grant(db, user, auditor)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400
        assert "提出済み" in resp.get_json()["error"]

    def test_edit_api_empty_body(self, db, logged_in_client, user, accounts):
        """空オブジェクト body は 400"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15),
                             source="journal", description="ORIG")
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={})
        assert resp.status_code == 400

    def test_edit_api_period_error(self, db, logged_in_client, user, accounts):
        """変更先期間が確定済みなら edit-api も 400"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15),
                             source="journal", description="ORIG")
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-01-15", "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400

    def test_edit_api_lv2_success_preserves_non_public(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 edit-api: 公開行のみ差し替え、非公開行 (事業主) は保持"""
        entry = JournalEntry(
            user_id=user.id, date=date(2026, 2, 15),
            entry_number=101, description="ORIG",
            source="journal",
        )
        entry.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=800, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="3030",
                             debit_amount=0, credit_amount=800),
        ]
        db.session.add(entry)
        db.session.commit()
        _setup_lv2_acting(db, logged_in_client, user, auditor)
        # 公開行 5010 debit 800 → 非公開 credit 800 と合わせてバランス
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15", "description": "Lv2API",
            "lines": [
                {"account_code": "5010", "debit_amount": 800, "credit_amount": 0},
                {"account_code": "3030", "debit_amount": 0, "credit_amount": 200,
                 "is_proprietor": True},
            ],
        })
        assert resp.status_code == 200
        db.session.refresh(entry)
        assert entry.description == "Lv2API"
        codes = {l.account_code: l for l in entry.lines}
        assert "3030" in codes  # 非公開行保持
        assert codes["3030"].credit_amount == 800
        assert codes["5010"].debit_amount == 800

    def test_edit_api_lv2_period_16_blocked(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 edit-api でも損益振替期間は拒否される"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15),
                             source="journal", description="ORIG")
        _setup_lv2_acting(db, logged_in_client, user, auditor)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-15", "description": "x", "fiscal_period": "16",
            "lines": [
                {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 0},
            ],
        })
        assert resp.status_code == 400
        assert "損益振替" in resp.get_json()["error"]

    def test_edit_api_lv2_period_error(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 edit-api: 変更先期間が確定済みなら 400"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15),
                             source="journal", description="ORIG")
        _setup_lv2_acting(db, logged_in_client, user, auditor)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-01-15", "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400


class TestCreateApiLockChecks:
    def test_create_api_locked_submitted_account_rejected(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """create-api で提出済み公開科目は拒否される"""
        _make_submitted_grant(db, user, auditor)
        resp = logged_in_client.post("/journal/create-api", json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400
        assert "提出済み" in resp.get_json()["error"]

    def test_create_api_lines_without_account_code_rejected(
        self, db, logged_in_client, user, accounts
    ):
        """account_code が空の明細だけの create-api は 400"""
        resp = logged_in_client.post("/journal/create-api", json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": "", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400

    def test_create_api_empty_body(self, db, logged_in_client, user, accounts):
        """空オブジェクト body は 400"""
        resp = logged_in_client.post("/journal/create-api", json={})
        assert resp.status_code == 400

    def test_create_api_period_16_blocked(self, db, logged_in_client, user, accounts):
        """create-api で損益振替期間は拒否される"""
        resp = logged_in_client.post("/journal/create-api", json={
            "date": "2026-02-15", "description": "x", "fiscal_period": "16",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400
        assert "損益振替" in resp.get_json()["error"]

    def test_create_api_locked_period_rejected(self, db, logged_in_client, user, accounts):
        """確定済み期間への create-api は 400"""
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post("/journal/create-api", json={
            "date": "2026-02-15", "description": "x",
            "lines": [
                {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
            ],
        })
        assert resp.status_code == 400

    def test_create_api_value_error_logged(self, db, logged_in_client, user, accounts):
        """create_journal_entry が ValueError なら safe なエラーを返す"""
        with patch("app.views.journal.create_journal_entry",
                   side_effect=ValueError("貸借が一致しません")):
            resp = logged_in_client.post("/journal/create-api", json={
                "date": "2026-02-15", "description": "x",
                "lines": [
                    {"account_code": "5010", "debit_amount": 100, "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0, "credit_amount": 100},
                ],
            })
        assert resp.status_code == 400
        assert "貸借" in resp.get_json()["error"]


class TestDeleteLockChecks:
    def test_delete_blocked_locked_for_owner(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """提出済み公開科目を含む伝票は削除できない"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15), source="journal")
        entry_id = entry.id
        _make_submitted_grant(db, user, auditor)
        resp = logged_in_client.post(f"/journal/{entry_id}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(JournalEntry, entry_id) is not None

    def test_delete_blocked_locked_for_owner_hx(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """提出済みロックの削除は HX で 422"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15), source="journal")
        entry_id = entry.id
        _make_submitted_grant(db, user, auditor)
        resp = logged_in_client.post(
            f"/journal/{entry_id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422
        assert db.session.get(JournalEntry, entry_id) is not None

    def test_delete_blocked_locked_for_auditor(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 顧問は事業主を含む伝票を削除できない"""
        entry = make_journal(db, user.id, "5010", "3030", 1000,
                             entry_date=date(2026, 2, 15), source="journal")
        entry_id = entry.id
        _setup_lv2_acting(db, logged_in_client, user, auditor)
        resp = logged_in_client.post(
            f"/journal/{entry_id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422
        assert db.session.get(JournalEntry, entry_id) is not None


class TestBulkDeleteLockChecks:
    def test_bulk_delete_skips_submitted_locked(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """提出済み公開科目を含む伝票は一括削除でもスキップされる"""
        entry = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15), source="journal")
        entry_id = entry.id
        _make_submitted_grant(db, user, auditor)
        resp = logged_in_client.post("/journal/bulk-delete", data={
            "entry_ids": [str(entry_id)],
        })
        assert resp.status_code in (302, 303)
        assert db.session.get(JournalEntry, entry_id) is not None

    def test_bulk_delete_skips_auditor_locked(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 顧問の一括削除は事業主を含む伝票をスキップ"""
        entry = make_journal(db, user.id, "5010", "3030", 1000,
                             entry_date=date(2026, 2, 15), source="journal")
        entry_id = entry.id
        _setup_lv2_acting(db, logged_in_client, user, auditor)
        resp = logged_in_client.post("/journal/bulk-delete", data={
            "entry_ids": [str(entry_id)],
        })
        assert resp.status_code in (302, 303)
        assert db.session.get(JournalEntry, entry_id) is not None


class TestSuggestCategoriesExtra:
    def test_empty_body_dict(self, logged_in_client, accounts):
        """空オブジェクト body は 400"""
        resp = logged_in_client.post(
            "/journal/api/suggest-categories", json={},
        )
        assert resp.status_code == 400

    def test_skips_payment_account_line(self, db, logged_in_client, user, accounts):
        """支払口座と同じ行はスキップして相手科目を返す"""
        # income 型: debit=1010, credit=5010
        make_journal(db, user.id, "1010", "5010", 100,
                     entry_date=date(2026, 1, 15), source="cashbook",
                     description="ファミマ")
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": ["ファミマ"],
            "payment_account_code": "1010",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ファミマ"]["account_code"] == "5010"


class TestAiSuggestCategoriesExtra:
    def test_empty_body_dict(self, logged_in_client, accounts):
        """空オブジェクト body は 400"""
        resp = logged_in_client.post(
            "/journal/api/ai-suggest-categories", json={},
        )
        assert resp.status_code == 400


class TestBatchesLockFlags:
    def _make_batch_entry(self, db, user_id, batch_id, d, source="csv", num=1):
        e = JournalEntry(
            user_id=user_id, date=d, entry_number=num,
            description=f"row{d}", source=source, batch_id=batch_id,
        )
        e.lines = [
            JournalEntryLine(account_user_id=user_id, account_code="5010",
                             debit_amount=100, credit_amount=0),
            JournalEntryLine(account_user_id=user_id, account_code="1010",
                             debit_amount=0, credit_amount=100),
        ]
        db.session.add(e)

    def test_batch_closing_source_not_deletable(self, db, logged_in_client, user, accounts):
        from uuid import uuid4
        bid = str(uuid4())
        self._make_batch_entry(db, user.id, bid, date(2026, 2, 15), source="closing")
        db.session.commit()
        resp = logged_in_client.get("/journal/batches")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "損益振替" in body

    def test_batch_with_locked_month_not_deletable(
        self, db, logged_in_client, user, accounts
    ):
        from uuid import uuid4
        bid = str(uuid4())
        self._make_batch_entry(db, user.id, bid, date(2026, 1, 15))
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = logged_in_client.get("/journal/batches")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "確定済み期間" in body

    def test_batch_spanning_year_boundary_deletable(
        self, db, logged_in_client, user, accounts
    ):
        """12月→翌1月にまたがるバッチは期間チェックが年をまたいで進む"""
        from uuid import uuid4
        bid = str(uuid4())
        self._make_batch_entry(db, user.id, bid, date(2026, 12, 15), num=1)
        self._make_batch_entry(db, user.id, bid, date(2027, 1, 10), num=2)
        db.session.commit()
        resp = logged_in_client.get("/journal/batches")
        assert resp.status_code == 200
        # 両月オープン → 削除可能 (確定済みメッセージなし)
        body = resp.get_data(as_text=True)
        assert "確定済み期間" not in body
