"""レポート (reports.py) の高度なエッジケース

期間範囲指定、ユーザー設定 (pref)、決算月、キャッシュ vs 非キャッシュ、
closing entries、事業科目折りたたみ等を網羅。

旧 Lv2 監査者向けの accounts_meta フィルタ / マスキング / ledger 403 のテストは
旧リアルタイム代理閲覧の撤去 (#112) に伴い削除した。
"""

from datetime import date

import pytest

from app.models.fiscal import FiscalClose
from tests.conftest import make_journal


class TestBalanceWithPref:
    def test_default_period_current_month(self, db, logged_in_client, user, accounts):
        user.set_pref("reports_default_period", "current_month")
        from app.extensions import db as _db
        _db.session.commit()
        # pf/pt 未指定 → current_month
        resp = logged_in_client.get("/reports/balance")
        assert resp.status_code == 200

    def test_default_period_all(self, db, logged_in_client, user, accounts):
        user.set_pref("reports_default_period", "all")
        from app.extensions import db as _db
        _db.session.commit()
        resp = logged_in_client.get("/reports/balance")
        assert resp.status_code == 200

    def test_period_range_with_pf_pt(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/balance?pf=3&pt=8")
        assert resp.status_code == 200

    def test_period_range_normalized(self, logged_in_client, accounts):
        # pf > pt → pt は pf に補正される
        resp = logged_in_client.get("/reports/balance?pf=8&pt=3")
        assert resp.status_code == 200

    def test_period_range_clamped(self, logged_in_client, accounts):
        # pf=-1 → 0、pt=99 → 16 にクランプ
        resp = logged_in_client.get("/reports/balance?pf=-1&pt=99")
        assert resp.status_code == 200

    def test_pt_includes_closing(self, db, logged_in_client, user, accounts):
        # pt=16 だと closing 仕訳も含む
        from app.models.journal import JournalEntry, JournalEntryLine
        e = JournalEntry(
            user_id=user.id,
            entry_number=1,
        )
        e.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=0, credit_amount=100),
            JournalEntryLine(account_user_id=user.id, account_code="3020",
                             debit_amount=100, credit_amount=0),
        ]
        db.session.add(e)
        db.session.commit()
        resp = logged_in_client.get("/reports/balance?pf=0&pt=16&year=2026")
        assert resp.status_code == 200

    def test_with_partial_close(self, db, logged_in_client, user, accounts):
        """E3-F-6: サーバ側残高集計は撤去済。確定期間が pf-1 に未到達でも 200。"""
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=1))
        db.session.commit()
        resp = logged_in_client.get("/reports/balance?pf=3&pt=8&year=2026")
        assert resp.status_code == 200

    def test_with_inactive_account(self, db, logged_in_client, user, accounts):
        # is_active=False だが deactivated_year >= year → 表示
        accounts["5020"].is_active = False
        accounts["5020"].deactivated_year = 2026
        from app.extensions import db as _db
        _db.session.commit()
        resp = logged_in_client.get("/reports/balance?year=2026")
        assert resp.status_code == 200


class TestBsAdvanced:
    def test_with_closing_entries(self, db, logged_in_client, user, accounts):
        from app.models.journal import JournalEntry, JournalEntryLine
        # closing 仕訳あり → has_closing=True で当期純利益を加算しない
        e = JournalEntry(
            user_id=user.id,
            entry_number=1,
        )
        e.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="3020",
                             debit_amount=10000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="4010",
                             debit_amount=0, credit_amount=10000),
        ]
        db.session.add(e)
        db.session.commit()
        resp = logged_in_client.get("/reports/bs?year=2026")
        assert resp.status_code == 200

    def test_with_revenue_and_expense(self, db, logged_in_client, user, accounts):
        # P/L 計算で純利益が出る状態
        make_journal(db, user.id, "1010", "4010", 100000,
                     entry_date=date(2026, 5, 15), source="cashbook")
        make_journal(db, user.id, "5010", "1010", 5000,
                     entry_date=date(2026, 5, 16), source="cashbook")
        resp = logged_in_client.get("/reports/bs?year=2026")
        assert resp.status_code == 200


class TestPlAdvanced:
    def test_with_month(self, db, logged_in_client, user, accounts):
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 5, 15), source="cashbook")
        resp = logged_in_client.get("/reports/pl?year=2026&month=5")
        assert resp.status_code == 200

    def test_with_month_12(self, db, logged_in_client, user, accounts):
        # 12月は次年へクロスオーバー
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 12, 15), source="cashbook")
        resp = logged_in_client.get("/reports/pl?year=2026&month=12")
        assert resp.status_code == 200


class TestLedgerAdvanced:
    def test_with_pref_default_period(self, db, logged_in_client, user, accounts):
        user.set_pref("reports_default_period", "current_month")
        from app.extensions import db as _db
        _db.session.commit()
        resp = logged_in_client.get("/reports/ledger")
        assert resp.status_code == 200

    def test_pf_pt_query(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/ledger?pf=3&pt=8")
        assert resp.status_code == 200

    def test_invalid_sort_falls_back(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/ledger?account_code=5010&sort=BAD")
        assert resp.status_code == 200

    def test_with_account_and_period_filter(self, db, logged_in_client, user, accounts):
        # E3-F-6: 旧 BalanceCache 経由の carry_forward テストは BCB クライアント
        # 描画に移行済 (#270)。サーバ側は HTML テンプレ + accountsMeta JSON を返すだけ。
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        resp = logged_in_client.get(
            "/reports/ledger?account_code=5010&pf=3&pt=8&year=2026"
        )
        assert resp.status_code == 200

    def test_bs_account_with_before_year_balance(self, db, logged_in_client, user, accounts):
        # 1010 = asset, 前年以前から残高あり
        make_journal(db, user.id, "1010", "3010", 50000,
                     entry_date=date(2025, 12, 31), source="cashbook")
        resp = logged_in_client.get(
            "/reports/ledger?account_code=1010&year=2026"
        )
        assert resp.status_code == 200

    def test_unknown_account_code(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/ledger?account_code=9999")
        assert resp.status_code == 200  # 結果は空

    def test_ledger_entries_meta_is_all_year_entries(self, db, logged_in_client, user, accounts):
        """#338 Phase R-2: entries_meta はサーバが平文 account_code で絞り込まず、
        年度内の全 entry のメタを返す (クライアントが復号して account_code で絞る)。
        選択科目 (5010) を含まない別仕訳 (1010↔4010) も entries_meta に含まれる。"""
        import json
        import re
        e_sel = make_journal(db, user.id, "5010", "1010", 1000,
                             entry_date=date(2026, 2, 15), source="cashbook")
        e_other = make_journal(db, user.id, "1010", "4010", 2000,
                               entry_date=date(2026, 3, 1), source="cashbook")
        resp = logged_in_client.get("/reports/ledger?account_code=5010&year=2026")
        assert resp.status_code == 200
        m = re.search(
            r'<script id="ledger-entries-meta"[^>]*>(.*?)</script>',
            resp.data.decode(), flags=re.DOTALL,
        )
        meta = json.loads(m.group(1).strip())
        # 選択科目 5010 を含む仕訳も、含まない仕訳も両方メタに入る (account_code 非依存)
        assert str(e_sel.id) in meta
        assert str(e_other.id) in meta


class TestMonthlyAdvanced:
    def test_with_data_and_projection(self, db, logged_in_client, user, accounts):
        # 当年データを入れて projection が出る状態
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(date.today().year, 1, 15), source="cashbook")
        resp = logged_in_client.get(f"/reports/monthly?year={date.today().year}")
        assert resp.status_code == 200

    def test_with_business_account(self, db, logged_in_client, user, accounts):
        from app.models.tax_form import TaxFormField, TaxFormMapping
        # 5010 を事業科目として登録
        field = TaxFormField(
            form_type="general", page=1, section="expenses",
            row_code="1", name="事業食費",
            account_type_code="expense", display_order=1,
        )
        db.session.add(field)
        db.session.flush()
        db.session.add(TaxFormMapping(
            user_id=user.id, account_code="5010",
            field_id=field.id,
        ))
        db.session.commit()
        make_journal(db, user.id, "5010", "1010", 5000,
                     entry_date=date(2026, 5, 15), source="cashbook")
        resp = logged_in_client.get("/reports/monthly?year=2026")
        assert resp.status_code == 200


class TestTaxFormReportAdvanced:
    def test_invalid_form_type(self, logged_in_client, accounts):
        resp = logged_in_client.get("/reports/tax-form?form_type=BAD")
        assert resp.status_code == 200  # general にフォールバック

    def test_with_data(self, db, logged_in_client, user, accounts):
        from app.models.tax_form import TaxFormField, TaxFormMapping
        field = TaxFormField(
            form_type="general", page=1, section="expenses",
            row_code="2", name="X",
            account_type_code="expense", display_order=1,
        )
        db.session.add(field)
        db.session.flush()
        db.session.add(TaxFormMapping(
            user_id=user.id, account_code="5010",
            field_id=field.id,
        ))
        db.session.commit()
        make_journal(db, user.id, "5010", "1010", 1000,
                     entry_date=date(2026, 5, 15), source="cashbook")
        resp = logged_in_client.get("/reports/tax-form?year=2026")
        assert resp.status_code == 200


