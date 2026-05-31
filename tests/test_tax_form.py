"""青色申告決算書マッピングのテスト"""
import pytest
from datetime import date

from app.models.tax_form import TaxFormField, TaxFormMapping
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.tax_form import (
    get_form_fields,
    get_mappable_fields,
    get_user_mappings,
    get_account_mapping,
    set_mapping,
    remove_mapping,
    bulk_create_accounts,
    save_mappings,
    get_business_account_codes,
    get_business_income,
)


@pytest.fixture
def tax_fields(db):
    """テスト用の決算書欄定義"""
    fields = [
        TaxFormField(
            form_type="general", page=1, section="revenue", row_code="1",
            name="売上（収入）金額", account_type_code="revenue",
            suggested_code="9010", display_order=10,
        ),
        TaxFormField(
            form_type="general", page=1, section="expenses", row_code="8",
            name="租税公課", account_type_code="expense",
            suggested_code="9210", display_order=100,
        ),
        TaxFormField(
            form_type="general", page=1, section="expenses", row_code="10",
            name="水道光熱費", account_type_code="expense",
            suggested_code="9230", display_order=120,
        ),
        TaxFormField(
            form_type="general", page=1, section="expenses", row_code="30",
            name="経費計", account_type_code="expense",
            is_subtotal=True, display_order=300,
        ),
        TaxFormField(
            form_type="general", page=4, section="bs_assets", row_code="A1",
            name="現金", account_type_code="asset",
            suggested_code="9510", display_order=400,
        ),
    ]
    db.session.add_all(fields)
    db.session.commit()
    return {f.row_code: f for f in fields}


class TestGetFormFields:
    def test_returns_all_fields(self, db, tax_fields):
        fields = get_form_fields("general")
        assert len(fields) == 5

    def test_ordered_by_display_order(self, db, tax_fields):
        fields = get_form_fields("general")
        assert fields[0].row_code == "1"
        assert fields[-1].row_code == "A1"


class TestGetMappableFields:
    def test_excludes_subtotals(self, db, tax_fields):
        fields = get_mappable_fields("general")
        assert len(fields) == 4
        assert all(not f.is_subtotal for f in fields)


class TestMapping:
    def test_set_and_get_mapping(self, db, user, accounts, tax_fields):
        field = tax_fields["1"]
        set_mapping(user.id, "4010", field.id)
        db.session.commit()

        result = get_account_mapping(user.id)
        assert result["4010"] == field.id

    def test_update_mapping(self, db, user, accounts, tax_fields):
        set_mapping(user.id, "5010", tax_fields["8"].id)
        db.session.commit()

        set_mapping(user.id, "5010", tax_fields["10"].id)
        db.session.commit()

        result = get_account_mapping(user.id)
        assert result["5010"] == tax_fields["10"].id

        # 1件だけ
        count = TaxFormMapping.query.filter_by(
            user_id=user.id, account_code="5010"
        ).count()
        assert count == 1

    def test_remove_mapping(self, db, user, accounts, tax_fields):
        set_mapping(user.id, "4010", tax_fields["1"].id)
        db.session.commit()

        remove_mapping(user.id, "4010")
        db.session.commit()

        result = get_account_mapping(user.id)
        assert "4010" not in result

    def test_get_user_mappings_grouped_by_field(self, db, user, accounts, tax_fields):
        set_mapping(user.id, "5010", tax_fields["8"].id)
        set_mapping(user.id, "5020", tax_fields["8"].id)
        db.session.commit()

        result = get_user_mappings(user.id)
        assert len(result[tax_fields["8"].id]) == 2
        assert "5010" in result[tax_fields["8"].id]
        assert "5020" in result[tax_fields["8"].id]


class TestBulkCreateAccounts:
    def test_creates_accounts_and_mappings(self, db, user, account_types, tax_fields):
        field_ids = [tax_fields["1"].id, tax_fields["8"].id]
        created, skipped = bulk_create_accounts(user.id, field_ids)
        db.session.commit()

        assert created == 2
        assert len(skipped) == 0

        # 科目が作成された
        acct = Account.query.filter_by(user_id=user.id, code="9010").first()
        assert acct is not None
        assert acct.name == "売上（収入）金額"
        assert acct.account_type.code == "revenue"

        # マッピングも設定された
        mapping = get_account_mapping(user.id)
        assert mapping["9010"] == tax_fields["1"].id
        assert mapping["9210"] == tax_fields["8"].id

    def test_skips_existing_code(self, db, user, accounts, account_types, tax_fields):
        # 9510 は既に存在しない想定で 1010(現金) をスキップするケースではなく
        # suggested_code="9510" で新規作成。コードが被らないので全部作成される。
        field_ids = [tax_fields["A1"].id]
        created, skipped = bulk_create_accounts(user.id, field_ids)
        db.session.commit()

        assert created == 1

    def test_skips_subtotal_fields(self, db, user, account_types, tax_fields):
        # 小計欄は除外される
        field_ids = [tax_fields["30"].id]
        created, skipped = bulk_create_accounts(user.id, field_ids)
        assert created == 0

    def test_existing_code_maps_only(self, db, user, account_types, tax_fields):
        # 先に科目を作成
        acct = Account(
            user_id=user.id, code="9010",
            account_type_id=account_types["revenue"].id,
            name="売上", is_active=True, display_order=10,
        )
        db.session.add(acct)
        db.session.commit()

        field_ids = [tax_fields["1"].id]
        created, skipped = bulk_create_accounts(user.id, field_ids)
        db.session.commit()

        assert created == 0
        assert "9010" in skipped

        # マッピングは設定された
        mapping = get_account_mapping(user.id)
        assert mapping["9010"] == tax_fields["1"].id


class TestSaveMappings:
    def test_replaces_all_mappings(self, db, user, accounts, tax_fields):
        # 初期マッピング
        set_mapping(user.id, "4010", tax_fields["1"].id)
        db.session.commit()

        # 新しいマッピングで上書き
        save_mappings(user.id, [
            {"account_code": "5010", "field_id": tax_fields["8"].id},
            {"account_code": "5020", "field_id": tax_fields["10"].id},
        ])
        db.session.commit()

        result = get_account_mapping(user.id)
        assert "4010" not in result  # 旧マッピングは削除
        assert result["5010"] == tax_fields["8"].id
        assert result["5020"] == tax_fields["10"].id


class TestSettingsView:
    def test_tax_form_page_loads(self, logged_in_client, db, tax_fields):
        resp = logged_in_client.get("/settings/tax-form")
        assert resp.status_code == 200
        assert "青色申告決算書" in resp.data.decode()

    def test_bulk_create_via_post(self, logged_in_client, db, user, account_types, tax_fields):
        field_ids = [str(tax_fields["1"].id), str(tax_fields["8"].id)]
        resp = logged_in_client.post(
            "/settings/tax-form/bulk-create",
            data={"field_ids": field_ids},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        acct = Account.query.filter_by(user_id=user.id, code="9010").first()
        assert acct is not None
        assert acct.name == "売上（収入）金額"

        acct2 = Account.query.filter_by(user_id=user.id, code="9210").first()
        assert acct2 is not None

    def test_save_mappings_via_post(self, logged_in_client, db, user, accounts, tax_fields):
        resp = logged_in_client.post(
            "/settings/tax-form/save-mappings",
            data={
                "mapping_4010": str(tax_fields["1"].id),
                "mapping_5010": str(tax_fields["8"].id),
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        mapping = get_account_mapping(user.id)
        assert mapping["4010"] == tax_fields["1"].id

    def test_settings_index_has_link(self, logged_in_client, db):
        resp = logged_in_client.get("/settings/")
        assert resp.status_code == 200
        assert "青色申告決算書" in resp.data.decode()

    def test_tax_form_tabs_shown(self, logged_in_client, db, tax_fields):
        """タブが表示され、一般用がアクティブ"""
        resp = logged_in_client.get("/settings/tax-form")
        html = resp.data.decode()
        assert "一般用" in html
        assert "不動産所得用" in html

    def test_tax_form_tab_general_active(self, logged_in_client, db, tax_fields):
        resp = logged_in_client.get("/settings/tax-form?form_type=general")
        html = resp.data.decode()
        # 一般用タブがアクティブ
        assert 'form_type=general' in html

    def test_tax_form_tab_real_estate(self, logged_in_client, db, tax_fields):
        """不動産所得用タブに切り替え可能"""
        resp = logged_in_client.get("/settings/tax-form?form_type=real_estate")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "不動産所得用" in html

    def test_bulk_create_preserves_form_type(self, logged_in_client, db, user, account_types, tax_fields):
        """一括作成後にform_typeが引き継がれる"""
        resp = logged_in_client.post(
            "/settings/tax-form/bulk-create",
            data={"field_ids": [str(tax_fields["1"].id)], "form_type": "general"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "form_type=general" in resp.headers["Location"]


class TestBusinessAccountCodes:
    def test_no_mappings_returns_empty(self, db, user, accounts):
        result = get_business_account_codes(user.id)
        assert result == set()

    def test_returns_mapped_codes(self, db, user, accounts, tax_fields):
        set_mapping(user.id, "4010", tax_fields["1"].id)
        set_mapping(user.id, "5010", tax_fields["8"].id)
        db.session.commit()

        result = get_business_account_codes(user.id)
        assert result == {"4010", "5010"}


class TestBusinessIncome:
    def test_no_mappings(self, db, user, accounts):
        result = get_business_income(user.id, 2026)
        assert result["has_mappings"] is False
        assert result["income"] == 0

    def test_calculates_income(self, db, user, accounts, account_types, tax_fields):
        # 事業用科目を作成してマッピング
        biz_rev = Account(
            user_id=user.id, code="9010", name="売上",
            account_type_id=account_types["revenue"].id,
            is_active=True, display_order=100,
        )
        biz_exp = Account(
            user_id=user.id, code="9210", name="租税公課",
            account_type_id=account_types["expense"].id,
            is_active=True, display_order=110,
        )
        db.session.add_all([biz_rev, biz_exp])
        db.session.commit()

        set_mapping(user.id, "9010", tax_fields["1"].id)
        set_mapping(user.id, "9210", tax_fields["8"].id)
        db.session.commit()

        # 仕訳: 売上 100,000
        entry1 = JournalEntry(
            user_id=user.id, date=date(2026, 3, 1),
            entry_number=1, description="売上", source="journal",
            fiscal_year=2026, fiscal_month=3,
        )
        entry1.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=100000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="9010",
                             debit_amount=0, credit_amount=100000),
        ]
        db.session.add(entry1)

        # 仕訳: 租税公課 30,000
        entry2 = JournalEntry(
            user_id=user.id, date=date(2026, 3, 5),
            entry_number=2, description="租税公課", source="journal",
            fiscal_year=2026, fiscal_month=3,
        )
        entry2.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="9210",
                             debit_amount=30000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=0, credit_amount=30000),
        ]
        db.session.add(entry2)
        db.session.commit()

        result = get_business_income(user.id, 2026)
        assert result["has_mappings"] is True
        assert result["revenue"] == 100000
        assert result["expense"] == 30000
        assert result["income"] == 70000

    def test_monthly_filter(self, db, user, accounts, account_types, tax_fields):
        biz_rev = Account(
            user_id=user.id, code="9010", name="売上",
            account_type_id=account_types["revenue"].id,
            is_active=True, display_order=100,
        )
        db.session.add(biz_rev)
        db.session.commit()
        set_mapping(user.id, "9010", tax_fields["1"].id)
        db.session.commit()

        for m in (1, 2, 3):
            entry = JournalEntry(
                user_id=user.id, date=date(2026, m, 15),
                entry_number=m, description=f"{m}月売上", source="journal",
                fiscal_year=2026, fiscal_month=m,
            )
            entry.lines = [
                JournalEntryLine(account_user_id=user.id, account_code="1010",
                                 debit_amount=10000, credit_amount=0),
                JournalEntryLine(account_user_id=user.id, account_code="9010",
                                 debit_amount=0, credit_amount=10000),
            ]
            db.session.add(entry)
        db.session.commit()

        result_year = get_business_income(user.id, 2026)
        assert result_year["revenue"] == 30000

        result_jan = get_business_income(user.id, 2026, 1)
        assert result_jan["revenue"] == 10000


class TestPLBusinessCollapse:
    """E3-F-3b 以降、P/L はクライアント描画。テストは accounts_meta JSON の
    is_business フラグと biz_income JSON の中身を検証する形に変えた。"""

    @staticmethod
    def _parse_pl(html):
        import json
        import re
        meta_m = re.search(
            r'<script id="pl-accounts-meta"[^>]*>(.*?)</script>',
            html, flags=re.DOTALL,
        )
        params_m = re.search(
            r'<script id="pl-server-params"[^>]*>(.*?)</script>',
            html, flags=re.DOTALL,
        )
        assert meta_m and params_m
        return (
            json.loads(meta_m.group(1).strip()),
            json.loads(params_m.group(1).strip()),
        )

    def test_pl_shows_business_income(self, logged_in_client, db, user, accounts, account_types, tax_fields):
        biz_rev = Account(
            user_id=user.id, code="9010", name="売上",
            account_type_id=account_types["revenue"].id,
            is_active=True, display_order=100,
        )
        db.session.add(biz_rev)
        db.session.commit()
        set_mapping(user.id, "9010", tax_fields["1"].id)
        db.session.commit()

        entry = JournalEntry(
            user_id=user.id, date=date(2026, 1, 15),
            entry_number=1, description="売上", source="journal",
            fiscal_year=2026, fiscal_month=1,
        )
        entry.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=200000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="9010",
                             debit_amount=0, credit_amount=200000),
        ]
        db.session.add(entry)
        db.session.commit()

        resp = logged_in_client.get("/reports/pl?year=2026")
        assert resp.status_code == 200
        meta, params = self._parse_pl(resp.data.decode())
        # 9010 は事業科目フラグ付き
        assert meta["9010"]["is_business"] is True
        # biz_income に集計が反映されている
        assert params["biz_income"]["has_mappings"] is True
        assert params["biz_income"]["income"] == 200000

    def test_pl_without_mappings_shows_all(self, logged_in_client, db, user, accounts, account_types):
        """マッピングなし時は biz_income.has_mappings=False で
        accounts_meta に is_business=True の科目は出ない"""
        entry = JournalEntry(
            user_id=user.id, date=date(2026, 1, 15),
            entry_number=1, description="給与", source="journal",
            fiscal_year=2026, fiscal_month=1,
        )
        entry.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=300000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="4010",
                             debit_amount=0, credit_amount=300000),
        ]
        db.session.add(entry)
        db.session.commit()

        resp = logged_in_client.get("/reports/pl?year=2026")
        assert resp.status_code == 200
        meta, params = self._parse_pl(resp.data.decode())
        # 給与収入 (4010) は通常科目
        assert "4010" in meta
        assert meta["4010"]["is_business"] is False
        # 事業マッピングなし → biz_income.has_mappings=False
        assert params["biz_income"]["has_mappings"] is False

    def test_pl_household_expense_not_hidden(self, logged_in_client, db, user, accounts, account_types, tax_fields):
        """事業科目をマッピングしても、家計科目は accounts_meta に残り、
        事業科目は is_business=True で client が除外する"""
        biz_exp = Account(
            user_id=user.id, code="9210", name="租税公課",
            account_type_id=account_types["expense"].id,
            is_active=True, display_order=110,
        )
        db.session.add(biz_exp)
        db.session.commit()
        set_mapping(user.id, "9210", tax_fields["8"].id)
        db.session.commit()

        e1 = JournalEntry(
            user_id=user.id, date=date(2026, 1, 10),
            entry_number=1, description="スーパー", source="journal",
            fiscal_year=2026, fiscal_month=1,
        )
        e1.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=5000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=0, credit_amount=5000),
        ]
        e2 = JournalEntry(
            user_id=user.id, date=date(2026, 1, 15),
            entry_number=2, description="税金", source="journal",
            fiscal_year=2026, fiscal_month=1,
        )
        e2.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="9210",
                             debit_amount=20000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=0, credit_amount=20000),
        ]
        db.session.add_all([e1, e2])
        db.session.commit()

        resp = logged_in_client.get("/reports/pl?year=2026")
        meta, _ = self._parse_pl(resp.data.decode())
        # 5010 (食費) は accounts_meta に残り、is_business=False
        assert "5010" in meta
        assert meta["5010"]["is_business"] is False
        # 9210 (租税公課) は accounts_meta に存在し is_business=True
        # (renderer 側で除外する)
        assert "9210" in meta
        assert meta["9210"]["is_business"] is True


class TestBusinessIncomeEdgeCases:
    def test_closing_entries_excluded(self, db, user, accounts, account_types, tax_fields):
        """source=closing の仕訳は事業所得計算から除外"""
        biz_rev = Account(
            user_id=user.id, code="9010", name="売上",
            account_type_id=account_types["revenue"].id,
            is_active=True, display_order=100,
        )
        db.session.add(biz_rev)
        db.session.commit()
        set_mapping(user.id, "9010", tax_fields["1"].id)
        db.session.commit()

        # 通常仕訳
        e1 = JournalEntry(user_id=user.id, date=date(2026, 6, 1),
                          entry_number=1, description="売上", source="journal",
            fiscal_year=2026, fiscal_month=6,
        )
        e1.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=100000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="9010",
                             debit_amount=0, credit_amount=100000),
        ]
        # closing 仕訳（除外されるべき）— E3-F: is_closing で識別する
        e2 = JournalEntry(user_id=user.id, date=date(2026, 12, 31),
                          entry_number=2, description="損益振替", source="closing",
                          is_closing=True, fiscal_month=16, fiscal_year=2026)
        e2.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="9010",
                             debit_amount=100000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="3020",
                             debit_amount=0, credit_amount=100000),
        ]
        db.session.add_all([e1, e2])
        db.session.commit()

        result = get_business_income(user.id, 2026)
        assert result["revenue"] == 100000  # closing は除外

    def test_business_loss(self, db, user, accounts, account_types, tax_fields):
        """事業損失（費用 > 収益）のケース"""
        biz_exp = Account(
            user_id=user.id, code="9210", name="租税公課",
            account_type_id=account_types["expense"].id,
            is_active=True, display_order=110,
        )
        db.session.add(biz_exp)
        db.session.commit()
        set_mapping(user.id, "9210", tax_fields["8"].id)
        db.session.commit()

        e1 = JournalEntry(user_id=user.id, date=date(2026, 3, 1),
                          entry_number=1, description="税金", source="journal",
            fiscal_year=2026, fiscal_month=3,
        )
        e1.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="9210",
                             debit_amount=50000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=0, credit_amount=50000),
        ]
        db.session.add(e1)
        db.session.commit()

        result = get_business_income(user.id, 2026)
        assert result["income"] == -50000  # マイナス = 事業損失


class TestSaveMappingsEdgeCases:
    def test_save_empty_clears_all(self, db, user, accounts, tax_fields):
        set_mapping(user.id, "4010", tax_fields["1"].id)
        db.session.commit()

        save_mappings(user.id, [])
        db.session.commit()

        result = get_account_mapping(user.id)
        assert len(result) == 0

    def test_save_with_invalid_field_id_ignored(self, db, user, accounts, tax_fields):
        """field_id が空の項目はスキップ"""
        save_mappings(user.id, [
            {"account_code": "4010", "field_id": tax_fields["1"].id},
            {"account_code": "5010", "field_id": ""},
            {"account_code": "", "field_id": tax_fields["8"].id},
        ])
        db.session.commit()

        result = get_account_mapping(user.id)
        assert len(result) == 1
        assert "4010" in result


class TestUserIsolation:
    """ユーザー分離テスト"""

    def test_mappings_isolated_between_users(self, db, user, second_user,
                                             accounts, second_user_accounts,
                                             account_types, tax_fields):
        """ユーザーAのマッピングはユーザーBに見えない"""
        set_mapping(user.id, "4010", tax_fields["1"].id)
        db.session.commit()

        result_a = get_account_mapping(user.id)
        result_b = get_account_mapping(second_user.id)

        assert "4010" in result_a
        assert len(result_b) == 0

    def test_business_codes_isolated(self, db, user, second_user,
                                     accounts, second_user_accounts,
                                     account_types, tax_fields):
        set_mapping(user.id, "5010", tax_fields["8"].id)
        db.session.commit()

        assert "5010" in get_business_account_codes(user.id)
        assert len(get_business_account_codes(second_user.id)) == 0

    def test_bulk_create_isolated(self, db, user, second_user,
                                  account_types, tax_fields):
        """一括作成は指定ユーザーにのみ科目を作成"""
        bulk_create_accounts(user.id, [tax_fields["1"].id])
        db.session.commit()

        assert Account.query.filter_by(user_id=user.id, code="9010").first() is not None
        assert Account.query.filter_by(user_id=second_user.id, code="9010").first() is None


class TestMonthlyCollapse:
    """E3-F-3d 以降、月次比較はクライアント描画。テストは accounts_meta JSON
    内の is_business フラグを検証する形に変えた (renderer が is_business=true
    の科目を biz_monthly 行として描画する)。"""

    @staticmethod
    def _parse_meta(html):
        import json
        import re
        m = re.search(
            r'<script id="monthly-accounts-meta"[^>]*>(.*?)</script>',
            html, flags=re.DOTALL,
        )
        assert m
        return json.loads(m.group(1).strip())

    def test_monthly_shows_biz_income_row(self, logged_in_client, db, user, accounts, account_types, tax_fields):
        """事業科目は accounts_meta に is_business=true で出る (renderer が
        biz_monthly 行を描画する)"""
        biz_rev = Account(
            user_id=user.id, code="9010", name="売上",
            account_type_id=account_types["revenue"].id,
            is_active=True, display_order=100,
        )
        db.session.add(biz_rev)
        db.session.commit()
        set_mapping(user.id, "9010", tax_fields["1"].id)
        db.session.commit()

        resp = logged_in_client.get("/reports/monthly?year=2026")
        assert resp.status_code == 200
        meta = self._parse_meta(resp.data.decode())
        assert meta["9010"]["is_business"] is True

    def test_monthly_without_mappings(self, logged_in_client, db, user, accounts, account_types):
        """マッピングなし時は accounts_meta に is_business=true の科目がない"""
        resp = logged_in_client.get("/reports/monthly?year=2026")
        assert resp.status_code == 200
        meta = self._parse_meta(resp.data.decode())
        assert not any(m.get("is_business") for m in meta.values())


class TestMultiFormTypeMapping:
    """複数form_type間の排他制御テスト（全科目が1つのform_typeにのみ所属）"""

    @pytest.fixture
    def re_fields(self, db):
        """不動産用の欄定義"""
        fields = {}
        defs = [
            ("real_estate", 1, "revenue", "1", "賃貸料", "revenue", "9510", False, 2000),
            ("real_estate", 4, "bs_assets", "A1", "現金", "asset", None, False, 2100),
        ]
        for ft, pg, sec, rc, nm, atc, sc, sub, do in defs:
            f = TaxFormField(
                form_type=ft, page=pg, section=sec, row_code=rc, name=nm,
                account_type_code=atc, suggested_code=sc, is_subtotal=sub,
                display_order=do,
            )
            db.session.add(f)
            fields[rc] = f
        db.session.commit()
        return fields

    def test_set_mapping_moves_to_new_form_type(self, db, user, accounts, account_types, tax_fields, re_fields):
        """set_mappingで他form_typeから自動削除（全科目共通）"""
        set_mapping(user.id, "4010", tax_fields["1"].id)
        db.session.commit()
        assert "4010" in get_account_mapping(user.id, "general")

        # 不動産用にマッピング → 一般用から自動削除
        set_mapping(user.id, "4010", re_fields["1"].id)
        db.session.commit()

        assert "4010" not in get_account_mapping(user.id, "general")
        assert "4010" in get_account_mapping(user.id, "real_estate")

    def test_asset_also_exclusive(self, db, user, accounts, account_types, tax_fields, re_fields):
        """資産科目も1つのform_typeにのみ（二重計上防止）"""
        general_bs = [f for f in tax_fields.values()
                      if f.section == "bs_assets" and not f.is_subtotal]
        assert general_bs

        set_mapping(user.id, "1010", general_bs[0].id)
        db.session.commit()
        assert "1010" in get_account_mapping(user.id, "general")

        # 不動産用にマッピング → 一般用から削除
        set_mapping(user.id, "1010", re_fields["A1"].id)
        db.session.commit()

        assert "1010" not in get_account_mapping(user.id, "general")
        assert "1010" in get_account_mapping(user.id, "real_estate")

    def test_save_mappings_removes_from_other_form_type(self, db, user, accounts, account_types, tax_fields, re_fields):
        """save_mappingsで科目が他form_typeから削除される"""
        set_mapping(user.id, "5010", re_fields["1"].id)
        db.session.commit()
        assert "5010" in get_account_mapping(user.id, "real_estate")

        # 一般用で同じ科目を保存 → 不動産用から削除
        save_mappings(user.id, [
            {"account_code": "5010", "field_id": tax_fields["8"].id},
        ], form_type="general")
        db.session.commit()

        assert "5010" in get_account_mapping(user.id, "general")
        assert "5010" not in get_account_mapping(user.id, "real_estate")

    def test_save_mappings_does_not_affect_unrelated(self, db, user, accounts, account_types, tax_fields, re_fields):
        """save_mappingsで無関係な科目の他form_typeマッピングは残る"""
        # 不動産用に4010をマッピング
        set_mapping(user.id, "4010", re_fields["1"].id)
        db.session.commit()

        # 一般用で5010のみ保存 → 不動産用の4010には影響なし
        save_mappings(user.id, [
            {"account_code": "5010", "field_id": tax_fields["8"].id},
        ], form_type="general")
        db.session.commit()

        assert "4010" in get_account_mapping(user.id, "real_estate")

    def test_set_mapping_replaces_within_form_type(self, db, user, accounts, tax_fields):
        """同一form_type内でset_mappingすると欄を変更"""
        set_mapping(user.id, "4010", tax_fields["1"].id)
        db.session.commit()

        set_mapping(user.id, "4010", tax_fields["8"].id)
        db.session.commit()

        mapping = get_account_mapping(user.id)
        assert mapping["4010"] == tax_fields["8"].id
        count = TaxFormMapping.query.filter_by(
            user_id=user.id, account_code="4010",
        ).count()
        assert count == 1
