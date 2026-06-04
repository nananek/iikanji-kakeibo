"""仕訳帳ビューの追加テスト

test_journal_views.py で扱った index/new/delete/bulk_delete に加えて、
get_json / suggest-categories / ai-suggest-categories / delete_batch を網羅。

E3-F PR-B2 以降、フォーム POST (edit) は廃止 (test_journal_views.py で 405 を担保)。
更新の本流は PUT /api/v1/journals/<id> (test_api.py::TestUpdateJournal)。元帳モーダル
経由の旧平文 /journal/<id>/edit-api は撤去済み (TestEditApiRemoved で 404/405 を担保)。
"""

from datetime import date

from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from tests.conftest import make_journal


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
        # E3-F PR-D-6-3b-3: 平文 date / description / fiscal_period / source は
        # 返さない (D-6-5 で DROP)。クライアントが blob を MK 復号して取り出す。
        assert "date" not in data
        assert "description" not in data
        assert "fiscal_period" not in data
        assert "source" not in data
        # blob / closing メタ + lines は引き続き返す。
        assert "encrypted_blob" in data
        assert "blob_iv" in data
        assert data["is_closing"] is False
        assert data["fiscal_year"] == 2026
        assert data["fiscal_month"] == 2
        assert len(data["lines"]) == 2
        # #338 item4: line は id + encrypted_blob/blob_iv のみ。平文 account_code /
        # debit / credit / description は返さない (元帳モーダルが fetchEntryForDiff で
        # 各 line blob を MK 復号して科目・金額・摘要を取得する)。
        for ln in data["lines"]:
            assert ln["id"] is not None
            assert "encrypted_blob" in ln
            assert "blob_iv" in ln
            assert "account_code" not in ln
            assert "debit_amount" not in ln
            assert "credit_amount" not in ln
            assert "description" not in ln

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


class TestEditApiRemoved:
    """旧 POST /journal/<id>/edit-api は撤去済み。

    仕訳編集はクライアント側 AES-GCM 暗号化 + PUT /api/v1/journals/<id> に
    一本化された (E2EE 平文 WRITE 停止)。サーバ側バリデーション (貸借・確定
    期間・提出ロック・損益振替拒否・IDOR) は test_api.py の PUT テストで網羅。
    ここでは旧エンドポイントが到達不能 (404/405) であることのみ担保する。
    """

    def _make_entry(self, db, user_id):
        return make_journal(db, user_id, "5010", "1010", 1000,
                            entry_date=date(2026, 2, 15),
                            source="journal", description="ORIG")

    def test_edit_api_removed(self, db, logged_in_client, user, accounts):
        entry = self._make_entry(db, user.id)
        resp = logged_in_client.post(f"/journal/{entry.id}/edit-api", json={
            "date": "2026-02-20",
            "description": "更新済み",
            "lines": [
                {"account_code": "5010", "debit_amount": 3000, "credit_amount": 0},
                {"account_code": "1010", "debit_amount": 0, "credit_amount": 3000},
            ],
        })
        # ルート自体が存在しないため POST は受け付けない (404 / 405)
        assert resp.status_code in (404, 405)


class TestSuggestCategoriesRemoved:
    """非AI /journal/api/suggest-categories は E3-F PR-D-4 で廃止。

    平文 description/date 読取を撤去し、クライアントが復号済み仕訳から推定する
    (crypto/suggest_categories_classical.js)。POST すると 404 を返すことを担保。
    """

    def test_endpoint_returns_404(self, logged_in_client, accounts):
        resp = logged_in_client.post("/journal/api/suggest-categories", json={
            "descriptions": ["ファミマ"], "payment_account_code": "1010",
        })
        assert resp.status_code == 404


class TestAiSuggestCategoriesRemoved:
    """/journal/api/ai-suggest-categories は廃止。
    POST すると 404 を返すことを担保。クライアントが直接
    /api/v1/suggest-categories/prompt-context + 自己 LLM 呼出で実行する。"""

    def test_endpoint_returns_404(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/journal/api/ai-suggest-categories",
            json={"payment_account_code": "1010", "rows": [{"description": "x"}]},
        )
        assert resp.status_code == 404


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
                user_id=user.id,
                entry_number=i + 1,
                batch_id=bid,
                # E3-F: 実エントリ同様に fiscal_year/fiscal_month を populate
                # (check_entry_modifiable は fiscal_year/fiscal_month を読む)。
                fiscal_year=2026, fiscal_month=2,
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
                user_id=user.id,
                entry_number=i + 1,
                batch_id=bid,
                fiscal_year=2026, fiscal_month=m,
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
        assert remaining[0].fiscal_month == 1
