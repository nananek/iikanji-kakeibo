"""POST /api/v1/fiscal/close-closing (#338 item1) のテスト。

決算月3 (period15) 確定 + クライアント暗号化生成した損益振替 (closing) 仕訳の
登録をアトミックに行う専用エンドポイント。汎用 batch API が拒否する
is_closing=True / fiscal_month=16 を本経路でのみ許可する。
"""

from app.models.journal import JournalEntry, JournalEntryLine
from app.models.fiscal import FiscalClose
from tests.conftest import _auth_header, encrypt_lines, encrypted_payload

URL = "/api/v1/fiscal/close-closing"


def _set_closed(db, user_id, period, year=2026):
    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    if fc is None:
        fc = FiscalClose(user_id=user_id, year=year, closed_period=period)
        db.session.add(fc)
    else:
        fc.closed_period = period
    db.session.commit()
    return fc


def _closing_entry(accounts, amount=1000):
    """収益 4010 を繰越利益 3020 へ振り替える balanced な closing entry payload。"""
    return {
        **encrypted_payload(),
        "lines": encrypt_lines([
            {"account_code": accounts["4010"].code, "debit": amount, "credit": 0},
            {"account_code": accounts["3020"].code, "debit": 0, "credit": amount},
        ]),
    }


class TestCloseClosingSuccess:
    def test_closing_entry_created_and_period_closed(self, client, db, user, accounts, auth_header):
        _set_closed(db, user.id, 14)
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts),
        })
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data["ok"] is True
        assert data["closed_period"] == 15
        assert data["closing_entry_id"] is not None
        # FiscalClose が 15 に進む
        fc = FiscalClose.query.filter_by(user_id=user.id, year=2026).first()
        assert fc.closed_period == 15
        # closing 仕訳が is_closing / fiscal_month=16 / encrypted_blob 非空で作られる
        entry = JournalEntry.query.get(data["closing_entry_id"])
        assert entry.is_closing is True
        assert entry.fiscal_month == 16
        assert entry.fiscal_year == 2026
        assert entry.encrypted_blob  # 非空 (旧サーバ生成の b"" センチネルでない)
        lines = JournalEntryLine.query.filter_by(journal_entry_id=entry.id).all()
        assert len(lines) == 2
        # #338 item5: line 本体は encrypted_blob のみ。平文 account_code/debit/credit
        # は書かれない (NULL)。貸借一致はクライアント + 監査時検査の責務。
        assert all(l.encrypted_blob for l in lines)
        assert all(l.debit_amount is None and l.credit_amount is None for l in lines)
        assert all(l.account_code is None for l in lines)

    def test_null_closing_entry_closes_period_only(self, client, db, user, accounts, auth_header):
        """振替不要 (closing_entry=null) なら period15 確定のみ。closing 仕訳 0 件。"""
        _set_closed(db, user.id, 14)
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": None,
        })
        assert resp.status_code == 200
        assert resp.get_json()["closing_entry_id"] is None
        fc = FiscalClose.query.filter_by(user_id=user.id, year=2026).first()
        assert fc.closed_period == 15
        assert JournalEntry.query.filter_by(user_id=user.id, is_closing=True).count() == 0

    def test_idempotent_replaces_existing_closing(self, client, db, user, accounts, auth_header):
        """既存の closing 仕訳があっても delete→再挿入で 1 件に保つ (冪等)。"""
        _set_closed(db, user.id, 14)
        # 残骸の closing 仕訳を手で作っておく (旧サーバ生成相当)
        stale = JournalEntry(user_id=user.id, entry_number=999, is_closing=True,
                             fiscal_month=16, fiscal_year=2026,
                             encrypted_blob=b"", blob_iv=bytes(12))
        db.session.add(stale)
        db.session.commit()
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts),
        })
        assert resp.status_code == 200
        closings = JournalEntry.query.filter_by(
            user_id=user.id, is_closing=True, fiscal_year=2026,
        ).all()
        assert len(closings) == 1
        assert closings[0].id == resp.get_json()["closing_entry_id"]


class TestCloseClosingValidation:
    def test_requires_period14_closed(self, client, db, user, accounts, auth_header):
        """決算月2 (period14) まで確定していなければ 409。FiscalClose 不変。"""
        _set_closed(db, user.id, 13)
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts),
        })
        assert resp.status_code == 409
        fc = FiscalClose.query.filter_by(user_id=user.id, year=2026).first()
        assert fc.closed_period == 13
        assert JournalEntry.query.filter_by(user_id=user.id, is_closing=True).count() == 0

    def test_no_fiscalclose_rejected(self, client, db, user, accounts, auth_header):
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts),
        })
        assert resp.status_code == 409

    def test_already_closed_rejected(self, client, db, user, accounts, auth_header):
        _set_closed(db, user.id, 15)
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts),
        })
        assert resp.status_code == 409
        assert "既に確定" in resp.get_json()["error"]

    def test_nonexistent_account_wire_ignored(self, client, db, user, accounts, auth_header):
        """#338 item5: closing 経路もサーバの科目存在検査を撤去 (クライアント +
        監査時検査の責務 §12.11/§13)。wire 上の存在しない account_code は無視され、
        closing 仕訳は 200 で受理される (line の account_code は NULL)。
        """
        _set_closed(db, user.id, 14)
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
        # closing 仕訳が作られ、決算月 15 へ進む
        fc = FiscalClose.query.filter_by(user_id=user.id, year=2026).first()
        assert fc.closed_period == 15
        assert JournalEntry.query.filter_by(user_id=user.id, is_closing=True).count() == 1

    def test_unbalanced_closing_wire_accepted(self, client, db, user, accounts, auth_header):
        """#338 item5: closing 経路もサーバ貸借検査を撤去。不一致に見える wire でも
        200 で受理される (金額は encrypted_blob、貸借はクライアント + 監査時検査)。
        """
        _set_closed(db, user.id, 14)
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
        fc = FiscalClose.query.filter_by(user_id=user.id, year=2026).first()
        assert fc.closed_period == 15

    def test_bad_blob_iv_length_rejected(self, client, db, user, accounts, auth_header):
        _set_closed(db, user.id, 14)
        entry = {
            "encrypted_blob": encrypted_payload()["encrypted_blob"],
            "blob_iv": encrypted_payload(n_blob_bytes=8)["encrypted_blob"],  # 48B(≠12)
            "lines": encrypt_lines([
                {"account_code": accounts["4010"].code, "debit": 1000, "credit": 0},
                {"account_code": accounts["3020"].code, "debit": 0, "credit": 1000},
            ]),
        }
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": entry,
        })
        assert resp.status_code == 400

    def test_empty_lines_rejected(self, client, db, user, accounts, auth_header):
        _set_closed(db, user.id, 14)
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": {**encrypted_payload(), "lines": []},
        })
        assert resp.status_code == 400

    def test_invalid_year_rejected(self, client, db, user, accounts, auth_header):
        resp = client.post(URL, headers=auth_header, json={
            "year": "2026", "closing_entry": None,
        })
        assert resp.status_code == 400

    def test_missing_body_rejected(self, client, db, user, accounts, auth_header):
        resp = client.post(URL, headers=auth_header, json=None)
        assert resp.status_code == 400


class TestCloseClosingIsolation:
    def test_only_affects_authenticated_user(self, client, db, user, accounts,
                                             second_user, second_user_accounts, auth_header):
        """auth_header は user のキー。second_user の period14 確定状態には影響しない。"""
        _set_closed(db, second_user.id, 14)        # 別ユーザーが14確定
        _set_closed(db, user.id, 14)               # 本人も14確定
        resp = client.post(URL, headers=auth_header, json={
            "year": 2026, "closing_entry": _closing_entry(accounts),
        })
        assert resp.status_code == 200
        # 本人は15、second_user は14のまま
        assert FiscalClose.query.filter_by(user_id=user.id, year=2026).first().closed_period == 15
        assert FiscalClose.query.filter_by(user_id=second_user.id, year=2026).first().closed_period == 14
        # closing 仕訳は本人のみ
        assert JournalEntry.query.filter_by(user_id=second_user.id, is_closing=True).count() == 0

    def test_requires_write_scope(self, client, db, user, accounts):
        """journals:read のみのキーでは closing 確定不可。"""
        from app.models.api_key import APIKey
        raw, key_hash, prefix = APIKey.generate()
        key = APIKey(user_id=user.id, name="ro", key_hash=key_hash,
                     key_prefix=prefix, scopes="journals:read", is_active=True)
        db.session.add(key)
        _set_closed(db, user.id, 14)
        resp = client.post(URL, headers=_auth_header(raw), json={
            "year": 2026, "closing_entry": None,
        })
        assert resp.status_code == 403
