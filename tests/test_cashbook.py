"""出納帳 (cashbook) ビューのテスト

cashbook.py のカバレッジ向上を目的とする。
"""

from datetime import date

from app.models.audit import AuditGrant, AuditGrantAccount
from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry
from tests.conftest import make_journal


def _setup_lv2(db, user, auditor, account_code="5010"):
    """Lv2 グラント + 公開科目を設定する"""
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
    return grant


def _act_as_lv2(logged_in_client, user, auditor, level=2):
    with logged_in_client.session_transaction() as sess:
        sess["_user_id"] = str(auditor.id)
        sess["acting_as_user_id"] = user.id
        sess["acting_as_permission_level"] = level


def _make_submitted_grant(db, user, auditor):
    """提出済み Lv2 グラント (公開科目 5010) を設定"""
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


class TestIndex:
    def test_unauthenticated_redirects(self, client):
        resp = client.get("/cashbook/")
        assert resp.status_code in (302, 401)

    def test_empty_index(self, logged_in_client, accounts):
        resp = logged_in_client.get("/cashbook/")
        assert resp.status_code == 200

    def test_lists_cashbook_entries(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        make_journal(db, user.id, "5010", "1010", 2000,
                     entry_date=date(2026, 2, 16), source="cashbook")
        # journal source は除外される
        make_journal(db, user.id, "5010", "1010", 500,
                     entry_date=date(2026, 2, 17), source="journal")

        resp = logged_in_client.get("/cashbook/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # 出納帳の 2 件は表示、journal の 500 円は表示されない
        assert "1,000" in body or "1000" in body
        assert "2,000" in body or "2000" in body

    def test_date_filter(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 1, 15), source="cashbook")
        make_journal(db, user.id, "5010", "1010", 2000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        resp = logged_in_client.get(
            "/cashbook/?date_from=2026-02-01&date_to=2026-02-28"
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "2,000" in body or "2000" in body

    def test_pagination(self, db, logged_in_client, user, accounts):
        for i in range(25):
            make_journal(db, user.id, "5010", "1010", 100 + i,
                         entry_date=date(2026, 2, i + 1), source="cashbook")
        resp1 = logged_in_client.get("/cashbook/?page=1")
        resp2 = logged_in_client.get("/cashbook/?page=2")
        assert resp1.status_code == 200
        assert resp2.status_code == 200

    def test_lv2_filter_shows_only_allowed_accounts(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2: 公開科目 (5010) を含む伝票のみ表示される"""
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 2, 15), source="cashbook")
        make_journal(db, user.id, "5020", "1010", 2000,
                     entry_date=date(2026, 2, 16), source="cashbook")
        _setup_lv2(db, user, auditor)
        _act_as_lv2(logged_in_client, user, auditor)
        resp = logged_in_client.get("/cashbook/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "1,000" in body or "1000" in body
        assert "2,000" not in body and "2000" not in body


class TestNewGet:
    def test_unauthenticated(self, client):
        resp = client.get("/cashbook/new")
        assert resp.status_code in (302, 401)

    def test_get_renders_form(self, logged_in_client, accounts):
        resp = logged_in_client.get("/cashbook/new")
        assert resp.status_code == 200


class TestNewPostExpense:
    def test_create_expense(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "1500",
            "description": "ランチ",
            "fiscal_period": "",
        })
        assert resp.status_code in (302, 303)
        entry = JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).first()
        assert entry is not None
        assert entry.description == "ランチ"

    def test_create_income(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "income",
            "payment_account_code": "1010",
            "category_account_code": "4010",
            "amount": "300000",
            "description": "給与",
            "fiscal_period": "",
        })
        assert resp.status_code in (302, 303)
        entry = JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).first()
        assert entry is not None
        assert entry.description == "給与"

    def test_create_transfer(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "transfer",
            "payment_account_code": "1010",
            "category_account_code": "1020",
            "amount": "10000",
            "description": "現金→預金",
            "fiscal_period": "",
        })
        assert resp.status_code in (302, 303)
        entry = JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).first()
        assert entry is not None

    def test_transfer_same_account_rejected(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "transfer",
            "payment_account_code": "1010",
            "category_account_code": "1010",
            "amount": "10000",
            "description": "同一",
            "fiscal_period": "",
        })
        assert resp.status_code == 200  # form 再表示
        body = resp.get_data(as_text=True)
        assert "異なる科目" in body
        # 仕訳は作成されていない
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).count() == 0

    def test_locked_period_rejected(self, db, logged_in_client, user, accounts):
        # 2026年2月を確定済みに
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "1000",
            "description": "確定済み",
            "fiscal_period": "",
        })
        # form 再表示で 200
        assert resp.status_code == 200
        # 仕訳は作成されていない
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).count() == 0

    def test_missing_required_fields(self, logged_in_client, accounts):
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "",  # 必須なのに空
            "transaction_type": "expense",
            "payment_account_code": "",
            "category_account_code": "",
            "amount": "",
            "description": "",
            "fiscal_period": "",
        })
        # 200 でフォーム再表示
        assert resp.status_code == 200

    def test_fiscal_period_special(self, db, logged_in_client, user, accounts):
        """期首振戻月 (fiscal_period=0) で登録"""
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "100",
            "description": "期首振戻",
            "fiscal_period": "0",
        })
        assert resp.status_code in (302, 303)
        entry = JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).first()
        assert entry is not None
        assert entry.fiscal_period == 0

    def test_new_locked_submitted_account_rejected(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """提出済み公開科目を含む新規登録は本人でも拒否される"""
        _make_submitted_grant(db, user, auditor)
        resp = logged_in_client.post("/cashbook/new", data={
            "date": "2026-02-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "1000",
            "description": "ロック科目",
            "fiscal_period": "",
        })
        assert resp.status_code == 200  # フォーム再表示
        # flash は showToast の tojson エスケープで埋め込まれる
        import json as _json
        escaped = _json.dumps("提出済みの税務科目を含むため登録できません。",
                              ensure_ascii=True)[1:-1]
        assert escaped in resp.get_data(as_text=True)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="cashbook"
        ).count() == 0


class TestEdit:
    def _make_cashbook(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1500,
                            entry_date=date(2026, 2, 15), source="cashbook",
                            description="編集対象")

    def test_unauthenticated(self, client):
        resp = client.get("/cashbook/1/edit")
        assert resp.status_code in (302, 401)

    def test_get_renders_form_with_existing(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "編集対象" in body

    def test_404_for_nonexistent(self, logged_in_client, accounts):
        resp = logged_in_client.get("/cashbook/9999/edit")
        assert resp.status_code == 404

    def test_idor_other_user(self, db, logged_in_client, user, accounts,
                             second_user, second_user_accounts):
        """他人の仕訳は 404"""
        other_entry = make_journal(
            db, second_user.id, "5010", "1010", 1000,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        resp = logged_in_client.get(f"/cashbook/{other_entry.id}/edit")
        assert resp.status_code == 404

    def test_post_updates_entry(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        resp = logged_in_client.post(f"/cashbook/{entry.id}/edit", data={
            "date": "2026-02-20",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "2500",
            "description": "更新後",
            "fiscal_period": "",
        })
        assert resp.status_code in (302, 303)
        db.session.refresh(entry)
        assert entry.description == "更新後"

    def test_edit_blocked_by_closed_period(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        # 確定済みなのでリダイレクト
        assert resp.status_code in (302, 303)

    def test_get_transfer_initial_values(self, db, logged_in_client, user, accounts):
        """資金移動仕訳の編集画面は移動元/移動先を初期値に表示"""
        # transfer: debit=1020 (BS), credit=1010 (BS)
        entry = make_journal(db, user.id, "1020", "1010", 10000,
                             entry_date=date(2026, 2, 15), source="cashbook",
                             description="振替")
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "tab: 'transfer'" in body
        assert "paymentCode: '1010'" in body
        assert "categoryCode: '1020'" in body

    def test_get_income_initial_values(self, db, logged_in_client, user, accounts):
        """収入仕訳の編集画面は入金先/収入源を初期値に表示"""
        # income: debit=1010 (BS), credit=4010 (revenue)
        entry = make_journal(db, user.id, "1010", "4010", 300000,
                             entry_date=date(2026, 2, 15), source="cashbook",
                             description="給与")
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "tab: 'income'" in body
        assert "paymentCode: '1010'" in body
        assert "categoryCode: '4010'" in body

    def test_post_transfer_updates(self, db, logged_in_client, user, accounts):
        """資金移動の更新で update_transfer_entry が呼ばれる"""
        entry = make_journal(db, user.id, "5010", "1010", 1500,
                             entry_date=date(2026, 2, 15), source="cashbook")
        resp = logged_in_client.post(f"/cashbook/{entry.id}/edit", data={
            "date": "2026-02-20",
            "transaction_type": "transfer",
            "payment_account_code": "1010",
            "category_account_code": "1020",
            "amount": "7000",
            "description": "振替更新",
            "fiscal_period": "0",
        })
        assert resp.status_code in (302, 303)
        db.session.refresh(entry)
        assert entry.fiscal_period == 0
        db.session.refresh(entry)
        lines = entry.lines
        assert len(lines) == 2
        debit = [l for l in lines if l.debit_amount > 0][0]
        credit = [l for l in lines if l.credit_amount > 0][0]
        assert debit.account_code == "1020"
        assert credit.account_code == "1010"
        assert debit.debit_amount == 7000
        assert entry.description == "振替更新"

    def test_post_transfer_same_account_rejected(
        self, db, logged_in_client, user, accounts
    ):
        """編集時も移動元=移動先は拒否してフォーム再表示"""
        entry = self._make_cashbook(db, user.id)
        resp = logged_in_client.post(f"/cashbook/{entry.id}/edit", data={
            "date": "2026-02-20",
            "transaction_type": "transfer",
            "payment_account_code": "1010",
            "category_account_code": "1010",
            "amount": "10000",
            "description": "同一",
            "fiscal_period": "",
        })
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "異なる科目" in body
        db.session.refresh(entry)
        assert entry.description == "編集対象"

    def test_post_period_error_redraws_form(
        self, db, logged_in_client, user, accounts
    ):
        """変更先の期間が確定済みならフォーム再表示"""
        entry = self._make_cashbook(db, user.id)  # 2026-02-15
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = logged_in_client.post(f"/cashbook/{entry.id}/edit", data={
            "date": "2026-01-15",
            "transaction_type": "expense",
            "payment_account_code": "1010",
            "category_account_code": "5010",
            "amount": "2500",
            "description": "確定済みへ",
            "fiscal_period": "",
        })
        assert resp.status_code == 200
        db.session.refresh(entry)
        assert entry.description == "編集対象"  # 更新されていない

    def _make_submitted_grant(self, db, user, auditor):
        """提出済み Lv2 グラント (公開科目 5010) を設定"""
        _make_submitted_grant(db, user, auditor)

    def test_edit_blocked_locked_for_owner(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """提出済み公開科目を含む伝票は本人でも編集できない"""
        entry = self._make_cashbook(db, user.id)  # 5010 を含む
        _make_submitted_grant(db, user, auditor)
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code in (302, 303)

    def test_edit_blocked_locked_for_auditor(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 顧問は非公開科目 (事業主) を含む伝票を編集できない"""
        # 事業主 3030 を含む伝票
        entry = make_journal(db, user.id, "5010", "3030", 1500,
                             entry_date=date(2026, 2, 15), source="cashbook")
        _setup_lv2(db, user, auditor)
        _act_as_lv2(logged_in_client, user, auditor)
        resp = logged_in_client.get(f"/cashbook/{entry.id}/edit")
        assert resp.status_code in (302, 303)


class TestDelete:
    def _make_cashbook(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1500,
                            entry_date=date(2026, 2, 15), source="cashbook")

    def test_unauthenticated(self, client):
        resp = client.post("/cashbook/1/delete")
        assert resp.status_code in (302, 401)

    def test_delete_success(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        entry_id = entry.id
        resp = logged_in_client.post(f"/cashbook/{entry_id}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(JournalEntry, entry_id) is None

    def test_delete_with_hx_request(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        entry_id = entry.id
        resp = logged_in_client.post(
            f"/cashbook/{entry_id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
        assert db.session.get(JournalEntry, entry_id) is None

    def test_delete_404_nonexistent(self, logged_in_client, accounts):
        resp = logged_in_client.post("/cashbook/9999/delete")
        assert resp.status_code == 404

    def test_idor_other_user_cannot_delete(self, db, logged_in_client, user, accounts,
                                            second_user, second_user_accounts):
        other_entry = make_journal(
            db, second_user.id, "5010", "1010", 1000,
            entry_date=date(2026, 2, 15), source="cashbook",
        )
        resp = logged_in_client.post(f"/cashbook/{other_entry.id}/delete")
        assert resp.status_code == 404
        # 残っている
        assert db.session.get(JournalEntry, other_entry.id) is not None

    def test_delete_blocked_by_closed_period(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(f"/cashbook/{entry.id}/delete")
        # 確定済みでリダイレクト or HX 422
        assert resp.status_code in (302, 303, 422)
        # 削除されていない
        assert db.session.get(JournalEntry, entry.id) is not None

    def test_delete_blocked_by_closed_period_hx(self, db, logged_in_client, user, accounts):
        entry = self._make_cashbook(db, user.id)
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.post(
            f"/cashbook/{entry.id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422
        assert "HX-Trigger" in resp.headers

    def test_delete_blocked_locked_for_owner(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """提出済み公開科目を含む伝票は削除できない (非 HX)"""
        entry = self._make_cashbook(db, user.id)
        entry_id = entry.id
        _make_submitted_grant(db, user, auditor)
        resp = logged_in_client.post(f"/cashbook/{entry_id}/delete")
        assert resp.status_code in (302, 303)
        assert db.session.get(JournalEntry, entry_id) is not None

    def test_delete_blocked_locked_for_owner_hx(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """提出済み公開科目を含む伝票は削除できない (HX: 422)"""
        entry = self._make_cashbook(db, user.id)
        entry_id = entry.id
        _make_submitted_grant(db, user, auditor)
        resp = logged_in_client.post(
            f"/cashbook/{entry_id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422
        assert "HX-Trigger" in resp.headers
        assert db.session.get(JournalEntry, entry_id) is not None

    def test_delete_blocked_locked_for_auditor_hx(
        self, db, logged_in_client, user, auditor, accounts
    ):
        """Lv2 顧問は非公開科目 (事業主) を含む伝票を削除できない"""
        entry = make_journal(db, user.id, "5010", "3030", 1500,
                             entry_date=date(2026, 2, 15), source="cashbook")
        entry_id = entry.id
        _setup_lv2(db, user, auditor)
        _act_as_lv2(logged_in_client, user, auditor)
        resp = logged_in_client.post(
            f"/cashbook/{entry_id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422
        assert "HX-Trigger" in resp.headers
        assert db.session.get(JournalEntry, entry_id) is not None
