"""POST /api/v1/fiscal/reencrypt-closing (#338 旧 closing 移行) のテスト。

item1 以前の旧 closing (encrypted_blob 空センチネル + 平文 line) を、クライアントが
再計算・暗号化した closing で in-place 置換する。FiscalClose は変えない (年度は既に
period15 確定済み)。
"""

from app.models.journal import JournalEntry, JournalEntryLine
from app.models.fiscal import FiscalClose
from tests.conftest import _auth_header, encrypt_lines, encrypted_payload

URL = "/api/v1/fiscal/reencrypt-closing"


def _set_closed(db, user_id, period=15, year=2026):
    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    if fc is None:
        fc = FiscalClose(user_id=user_id, year=year, closed_period=period)
        db.session.add(fc)
    else:
        fc.closed_period = period
    db.session.commit()
    return fc


def _make_old_closing(db, user_id, year=2026, amount=1000, codes=("4010", "3020")):
    """item1 以前の旧 closing (空 blob + 平文 line) を直接 INSERT する。codes は
    そのユーザーに存在する (借方科目, 貸方科目)。"""
    e = JournalEntry(user_id=user_id, entry_number=900 + (year % 100), is_closing=True,
                     fiscal_month=16, fiscal_year=year,
                     encrypted_blob=b"", blob_iv=bytes(12))
    e.lines = [
        JournalEntryLine(account_user_id=user_id,
                         encrypted_blob=b"", blob_iv=bytes(12)),
        JournalEntryLine(account_user_id=user_id,
                         encrypted_blob=b"", blob_iv=bytes(12)),
    ]
    db.session.add(e)
    db.session.commit()
    return e


def _closing_entry(accounts, amount=1000):
    return {
        **encrypted_payload(),
        "lines": encrypt_lines([
            {"account_code": accounts["4010"].code, "debit": amount, "credit": 0},
            {"account_code": accounts["3020"].code, "debit": 0, "credit": amount},
        ]),
    }


class TestReencryptSuccess:
    def test_replaces_old_closing_with_encrypted(self, client, db, user, accounts, auth_header):
        _set_closed(db, user.id, 15)
        _make_old_closing(db, user.id)
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts),
        })
        assert resp.status_code == 200, resp.get_json()
        new_id = resp.get_json()["closing_entry_id"]
        assert new_id is not None
        # 旧 closing (空 blob) は消え、新 closing 1 件 (実 blob)
        closings = JournalEntry.query.filter_by(
            user_id=user.id, is_closing=True, fiscal_year=2026,
        ).all()
        assert len(closings) == 1
        assert closings[0].id == new_id
        assert closings[0].encrypted_blob  # 非空 (旧センチネル b"" でない)
        assert closings[0].fiscal_month == 16
        # FiscalClose は不変 (15 のまま)
        fc = FiscalClose.query.filter_by(user_id=user.id, year=2026).first()
        assert fc.closed_period == 15

    def test_reencrypted_lines_store_blob_not_plaintext(self, client, db, user, accounts, auth_header):
        """#338 item5/Phase8: 再暗号化 closing の line は encrypted_blob のみを持つ。
        平文 debit/credit/account_code 列は物理削除済みでサーバは平文を保持しない。
        金額の faithful 性はクライアントが encrypted_blob 内で担保する。"""
        _set_closed(db, user.id, 15)
        _make_old_closing(db, user.id, amount=7777)
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts, amount=7777),
        })
        assert resp.status_code == 200
        new = JournalEntry.query.get(resp.get_json()["closing_entry_id"])
        assert new.lines
        for l in new.lines:
            assert l.encrypted_blob  # 非空 (クライアント暗号化済 line 本体)

    def test_idempotent(self, client, db, user, accounts, auth_header):
        _set_closed(db, user.id, 15)
        _make_old_closing(db, user.id)
        for _ in range(2):
            resp = client.post(URL, headers=auth_header, json={
                "year": 2026, "closing_entry": _closing_entry(accounts),
            })
            assert resp.status_code == 200
        assert JournalEntry.query.filter_by(
            user_id=user.id, is_closing=True, fiscal_year=2026,
        ).count() == 1

    def test_null_closing_entry_deletes_only(self, client, db, user, accounts, auth_header):
        """再計算がゼロ (closing_entry=null) なら旧 closing 削除のみ。"""
        _set_closed(db, user.id, 15)
        _make_old_closing(db, user.id)
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": None,
        })
        assert resp.status_code == 200
        assert resp.get_json()["closing_entry_id"] is None
        assert JournalEntry.query.filter_by(
            user_id=user.id, is_closing=True, fiscal_year=2026,
        ).count() == 0
        # FiscalClose は不変
        assert FiscalClose.query.filter_by(user_id=user.id, year=2026).first().closed_period == 15


class TestReencryptValidation:
    def test_requires_period15_closed(self, client, db, user, accounts, auth_header):
        _set_closed(db, user.id, 14)
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts),
        })
        assert resp.status_code == 409

    def test_no_fiscalclose_rejected(self, client, db, user, accounts, auth_header):
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts),
        })
        assert resp.status_code == 409

    def test_nonexistent_account_wire_ignored(self, client, db, user, accounts, auth_header):
        """#338 item5: reencrypt 経路もサーバ科目存在検査を撤去。存在しない
        account_code を wire に乗せても 200 で受理され、旧 closing は新 closing
        (実 blob) に置換される (科目存在はクライアント + 監査時検査)。"""
        _set_closed(db, user.id, 15)
        _make_old_closing(db, user.id)
        entry = {
            **encrypted_payload(),
            "lines": encrypt_lines([
                {"account_code": "9999", "debit": 1000, "credit": 0},
                {"account_code": accounts["3020"].code, "debit": 0, "credit": 1000},
            ]),
        }
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": entry,
        })
        assert resp.status_code == 200
        closings = JournalEntry.query.filter_by(user_id=user.id, is_closing=True, fiscal_year=2026).all()
        assert len(closings) == 1
        assert closings[0].encrypted_blob  # 新 closing (旧 b"" センチネルでない)

    def test_unbalanced_wire_accepted(self, client, db, user, accounts, auth_header):
        """#338 item5: reencrypt 経路もサーバ貸借検査を撤去。不一致に見える wire でも
        200 で受理される (金額は encrypted_blob、貸借はクライアント + 監査時検査)。"""
        _set_closed(db, user.id, 15)
        _make_old_closing(db, user.id)
        entry = {
            **encrypted_payload(),
            "lines": encrypt_lines([
                {"account_code": accounts["4010"].code, "debit": 1000, "credit": 0},
                {"account_code": accounts["3020"].code, "debit": 0, "credit": 800},
            ]),
        }
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": entry,
        })
        assert resp.status_code == 200

    def test_invalid_year(self, client, db, user, accounts, auth_header):
        resp = client.post(URL, headers=auth_header, json={"year": "x", "closing_entry": None})
        assert resp.status_code == 400

    def test_missing_body(self, client, db, user, accounts, auth_header):
        resp = client.post(URL, headers=auth_header, json=None)
        assert resp.status_code == 400


class TestReencryptIsolation:
    def test_only_affects_authenticated_user(self, client, db, user, accounts,
                                             second_user, second_user_accounts, auth_header):
        _set_closed(db, second_user.id, 15)
        _make_old_closing(db, second_user.id, codes=("5010", "1010"))  # second_user の科目
        _set_closed(db, user.id, 15)
        _make_old_closing(db, user.id)
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts),
        })
        assert resp.status_code == 200
        # second_user の旧 closing は不変 (空 blob のまま)
        other = JournalEntry.query.filter_by(
            user_id=second_user.id, is_closing=True, fiscal_year=2026,
        ).all()
        assert len(other) == 1
        assert other[0].encrypted_blob == b""

    def test_requires_write_scope(self, client, db, user, accounts):
        from app.models.api_key import APIKey
        raw, key_hash, prefix = APIKey.generate()
        db.session.add(APIKey(user_id=user.id, name="ro", key_hash=key_hash,
                              key_prefix=prefix, scopes="journals:read", is_active=True))
        _set_closed(db, user.id, 15)
        db.session.commit()
        resp = client.post(URL, headers=_auth_header(raw), json={"year": 2026, "closing_entry": None})
        assert resp.status_code == 403


class TestOldClosingYearsHelper:
    def test_lists_only_old_closing_years(self, db, user, accounts):
        """_old_closing_years は空 blob の closing を持つ年度のみ列挙し、新 closing
        (実 blob) の年度は含めない (移行 UI の対象年度供給)。"""
        from app.views.settings import _old_closing_years
        _make_old_closing(db, user.id, year=2025)
        _make_old_closing(db, user.id, year=2023)
        # 新 closing (実 blob) の年度は対象外
        e = JournalEntry(user_id=user.id, entry_number=950, is_closing=True,
                         fiscal_month=16, fiscal_year=2024,
                         encrypted_blob=b"realblob", blob_iv=bytes(12))
        e.lines = [JournalEntryLine(account_user_id=user.id,
                                    encrypted_blob=b"x", blob_iv=bytes(12)),
                   JournalEntryLine(account_user_id=user.id,
                                    encrypted_blob=b"x", blob_iv=bytes(12))]
        db.session.add(e)
        db.session.commit()
        assert _old_closing_years(user.id) == [2023, 2025]  # 昇順・2024 (実blob) 除外

    def test_empty_when_no_old_closing(self, db, user, accounts):
        from app.views.settings import _old_closing_years
        assert _old_closing_years(user.id) == []


class TestMigrationBanner:
    def test_banner_shown_with_old_closing(self, db, logged_in_client, user, accounts):
        """旧 closing があると fiscal ページに移行バナー + closing-migration-params が出る。"""
        _set_closed(db, user.id, 15, year=2025)
        _make_old_closing(db, user.id, year=2025)
        resp = logged_in_client.get("/settings/fiscal?year=2025")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "closing-migrate-btn" in body
        assert "closing_migration_hook.mjs" in body
        import re, json as _json
        m = re.search(r'id="closing-migration-params"[^>]*>\s*(\{.*?\})\s*<', body, re.DOTALL)
        assert m, "closing-migration-params が埋め込まれていない"
        assert _json.loads(m.group(1))["years"] == [2025]

    def test_no_banner_without_old_closing(self, db, logged_in_client, user, accounts):
        resp = logged_in_client.get("/settings/fiscal?year=2026")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "closing-migrate-btn" not in body
        assert "closing_migration_hook.mjs" not in body
