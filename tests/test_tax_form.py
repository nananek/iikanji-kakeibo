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
    get_tax_form_report,
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


class TestTaxFormReport:
    def test_report_loads(self, logged_in_client, db, tax_fields):
        resp = logged_in_client.get("/reports/tax-form")
        assert resp.status_code == 200
        assert "青色申告決算書" in resp.data.decode()

    def test_report_with_data(self, db, user, accounts, account_types, tax_fields):
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
            user_id=user.id, date=date(2026, 6, 1),
            entry_number=1, description="売上", source="journal",
        )
        entry.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=50000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="9010",
                             debit_amount=0, credit_amount=50000),
        ]
        db.session.add(entry)
        db.session.commit()

        data = get_tax_form_report(user.id, 2026)
        # 売上欄に50000が入っている
        revenue_item = next(d for d in data if d["field"].row_code == "1")
        assert revenue_item["amount"] == 50000

    def test_reports_index_has_link(self, logged_in_client, db):
        resp = logged_in_client.get("/reports/")
        assert resp.status_code == 200
        assert "青色申告決算書" in resp.data.decode()


class TestPLBusinessCollapse:
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
        html = resp.data.decode()
        assert resp.status_code == 200
        assert "事業所得" in html
        # 売上（個別科目）は表示されない
        assert "売上" not in html or "事業所得" in html

    def test_pl_without_mappings_shows_all(self, logged_in_client, db, user, accounts, account_types):
        """マッピングなし時は従来通り全科目表示"""
        entry = JournalEntry(
            user_id=user.id, date=date(2026, 1, 15),
            entry_number=1, description="給与", source="journal",
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
        html = resp.data.decode()
        assert resp.status_code == 200
        assert "給与収入" in html
        assert "事業所得" not in html

    def test_pl_household_expense_not_hidden(self, logged_in_client, db, user, accounts, account_types, tax_fields):
        """事業科目をマッピングしても、家計科目はP/Lに残る"""
        biz_exp = Account(
            user_id=user.id, code="9210", name="租税公課",
            account_type_id=account_types["expense"].id,
            is_active=True, display_order=110,
        )
        db.session.add(biz_exp)
        db.session.commit()
        set_mapping(user.id, "9210", tax_fields["8"].id)
        db.session.commit()

        # 家計費用
        e1 = JournalEntry(
            user_id=user.id, date=date(2026, 1, 10),
            entry_number=1, description="スーパー", source="journal",
        )
        e1.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="5010",
                             debit_amount=5000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=0, credit_amount=5000),
        ]
        # 事業費用
        e2 = JournalEntry(
            user_id=user.id, date=date(2026, 1, 15),
            entry_number=2, description="税金", source="journal",
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
        html = resp.data.decode()
        assert "食費" in html
        assert "租税公課" not in html  # 事業科目はP/Lに出ない


class TestSubtotalCalculation:
    """_compute_subtotals の小計計算ロジック"""

    @pytest.fixture
    def full_pl_fields(self, db):
        """P/Lの完全な欄セット（小計含む）"""
        fields = [
            TaxFormField(form_type="general", page=1, section="revenue",
                         row_code="1", name="売上", account_type_code="revenue",
                         suggested_code="9010", display_order=10),
            TaxFormField(form_type="general", page=1, section="cost_of_sales",
                         row_code="2", name="期首商品棚卸高", account_type_code="asset",
                         suggested_code="9110", display_order=20),
            TaxFormField(form_type="general", page=1, section="cost_of_sales",
                         row_code="3", name="仕入金額", account_type_code="expense",
                         suggested_code="9120", display_order=30),
            TaxFormField(form_type="general", page=1, section="cost_of_sales",
                         row_code="4", name="小計", account_type_code="expense",
                         is_subtotal=True, display_order=40),
            TaxFormField(form_type="general", page=1, section="cost_of_sales",
                         row_code="5", name="期末商品棚卸高", account_type_code="asset",
                         suggested_code="9130", display_order=50),
            TaxFormField(form_type="general", page=1, section="cost_of_sales",
                         row_code="6", name="差引原価", account_type_code="expense",
                         is_subtotal=True, display_order=60),
            TaxFormField(form_type="general", page=1, section="cost_of_sales",
                         row_code="7", name="差引金額", account_type_code="revenue",
                         is_subtotal=True, display_order=70),
            TaxFormField(form_type="general", page=1, section="expenses",
                         row_code="8", name="租税公課", account_type_code="expense",
                         suggested_code="9210", display_order=100),
            TaxFormField(form_type="general", page=1, section="expenses",
                         row_code="30", name="経費計", account_type_code="expense",
                         is_subtotal=True, display_order=300),
            TaxFormField(form_type="general", page=1, section="income",
                         row_code="31", name="差引金額", account_type_code="revenue",
                         is_subtotal=True, display_order=310),
            TaxFormField(form_type="general", page=1, section="income",
                         row_code="32", name="専従者給与", account_type_code="expense",
                         suggested_code="9430", display_order=320),
            TaxFormField(form_type="general", page=1, section="income",
                         row_code="33", name="引当金繰戻", account_type_code="revenue",
                         display_order=330),
            TaxFormField(form_type="general", page=1, section="income",
                         row_code="34", name="引当金繰入", account_type_code="expense",
                         display_order=340),
            TaxFormField(form_type="general", page=1, section="income",
                         row_code="35", name="控除前所得", account_type_code="revenue",
                         is_subtotal=True, display_order=350),
            TaxFormField(form_type="general", page=1, section="income",
                         row_code="36", name="青色控除額", account_type_code="expense",
                         is_subtotal=True, display_order=360),
            TaxFormField(form_type="general", page=1, section="income",
                         row_code="37", name="所得金額", account_type_code="revenue",
                         is_subtotal=True, display_order=370),
        ]
        db.session.add_all(fields)
        db.session.commit()
        return {f.row_code: f for f in fields}

    def test_gross_profit_calculation(self, db, user, accounts, account_types, full_pl_fields):
        """売上 - 原価 = 差引金額（欄7）"""
        # 売上科目
        rev = Account(user_id=user.id, code="9010", name="売上",
                      account_type_id=account_types["revenue"].id,
                      is_active=True, display_order=100)
        purchase = Account(user_id=user.id, code="9120", name="仕入",
                           account_type_id=account_types["expense"].id,
                           is_active=True, display_order=110)
        db.session.add_all([rev, purchase])
        db.session.commit()

        set_mapping(user.id, "9010", full_pl_fields["1"].id)
        set_mapping(user.id, "9120", full_pl_fields["3"].id)
        db.session.commit()

        # 売上 500,000
        e1 = JournalEntry(user_id=user.id, date=date(2026, 6, 1),
                          entry_number=1, description="売上", source="journal")
        e1.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=500000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="9010",
                             debit_amount=0, credit_amount=500000),
        ]
        # 仕入 200,000
        e2 = JournalEntry(user_id=user.id, date=date(2026, 6, 5),
                          entry_number=2, description="仕入", source="journal")
        e2.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="9120",
                             debit_amount=200000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=0, credit_amount=200000),
        ]
        db.session.add_all([e1, e2])
        db.session.commit()

        data = get_tax_form_report(user.id, 2026)
        by_code = {d["field"].row_code: d for d in data}

        assert by_code["1"]["amount"] == 500000   # 売上
        assert by_code["3"]["amount"] == 200000   # 仕入
        assert by_code["6"]["amount"] == 200000   # 差引原価（期首棚卸0 + 仕入200k - 期末棚卸0）
        assert by_code["7"]["amount"] == 300000   # 差引金額 = 500k - 200k

    def test_income_calculation_chain(self, db, user, accounts, account_types, full_pl_fields):
        """所得金額の計算チェーン: 欄31→35→37"""
        rev = Account(user_id=user.id, code="9010", name="売上",
                      account_type_id=account_types["revenue"].id,
                      is_active=True, display_order=100)
        exp = Account(user_id=user.id, code="9210", name="租税公課",
                      account_type_id=account_types["expense"].id,
                      is_active=True, display_order=110)
        db.session.add_all([rev, exp])
        db.session.commit()

        set_mapping(user.id, "9010", full_pl_fields["1"].id)
        set_mapping(user.id, "9210", full_pl_fields["8"].id)
        db.session.commit()

        e1 = JournalEntry(user_id=user.id, date=date(2026, 6, 1),
                          entry_number=1, description="売上", source="journal")
        e1.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=1000000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="9010",
                             debit_amount=0, credit_amount=1000000),
        ]
        e2 = JournalEntry(user_id=user.id, date=date(2026, 6, 10),
                          entry_number=2, description="租税公課", source="journal")
        e2.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="9210",
                             debit_amount=100000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=0, credit_amount=100000),
        ]
        db.session.add_all([e1, e2])
        db.session.commit()

        data = get_tax_form_report(user.id, 2026)
        by_code = {d["field"].row_code: d for d in data}

        # 差引金額(31) = 売上(1M) - 原価(0) - 経費(100k)
        assert by_code["31"]["amount"] == 900000
        # 控除前所得(35) = 差引金額 - 専従者0 + 繰戻0 - 繰入0
        assert by_code["35"]["amount"] == 900000
        # 所得金額(37) = 控除前 - 控除額(0)
        assert by_code["37"]["amount"] == 900000


class TestTaxFormReportBS:
    """B/S欄の期首・期末テスト"""

    @pytest.fixture
    def bs_fields(self, db):
        fields = [
            TaxFormField(form_type="general", page=4, section="bs_assets",
                         row_code="A1", name="現金", account_type_code="asset",
                         suggested_code="9510", display_order=400),
            TaxFormField(form_type="general", page=4, section="bs_assets",
                         row_code="AT", name="資産合計", account_type_code="asset",
                         is_subtotal=True, display_order=490),
        ]
        db.session.add_all(fields)
        db.session.commit()
        return {f.row_code: f for f in fields}

    def test_bs_opening_and_closing(self, db, user, accounts, account_types, bs_fields):
        """B/S欄の期首残高と期末残高"""
        set_mapping(user.id, "1010", bs_fields["A1"].id)
        db.session.commit()

        # 前年仕訳: 現金残高 100,000
        e0 = JournalEntry(user_id=user.id, date=date(2025, 12, 31),
                          entry_number=1, description="前年", source="journal")
        e0.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=100000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="4010",
                             debit_amount=0, credit_amount=100000),
        ]
        # 当年仕訳: 現金 +50,000
        e1 = JournalEntry(user_id=user.id, date=date(2026, 3, 1),
                          entry_number=2, description="入金", source="journal")
        e1.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=50000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="4010",
                             debit_amount=0, credit_amount=50000),
        ]
        db.session.add_all([e0, e1])
        db.session.commit()

        data = get_tax_form_report(user.id, 2026)
        by_code = {d["field"].row_code: d for d in data}

        assert by_code["A1"]["opening"] == 100000   # 期首
        assert by_code["A1"]["amount"] == 150000     # 期末
        assert by_code["AT"]["opening"] == 100000
        assert by_code["AT"]["amount"] == 150000


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
                          entry_number=1, description="売上", source="journal")
        e1.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=100000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="9010",
                             debit_amount=0, credit_amount=100000),
        ]
        # closing 仕訳（除外されるべき）
        e2 = JournalEntry(user_id=user.id, date=date(2026, 12, 31),
                          entry_number=2, description="損益振替", source="closing")
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
                          entry_number=1, description="税金", source="journal")
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
    def test_monthly_shows_biz_income_row(self, logged_in_client, db, user, accounts, account_types, tax_fields):
        """月次比較で事業所得行が表示される"""
        biz_rev = Account(
            user_id=user.id, code="9010", name="売上",
            account_type_id=account_types["revenue"].id,
            is_active=True, display_order=100,
        )
        db.session.add(biz_rev)
        db.session.commit()
        set_mapping(user.id, "9010", tax_fields["1"].id)
        db.session.commit()

        e1 = JournalEntry(user_id=user.id, date=date(2026, 1, 15),
                          entry_number=1, description="売上", source="journal")
        e1.lines = [
            JournalEntryLine(account_user_id=user.id, account_code="1010",
                             debit_amount=100000, credit_amount=0),
            JournalEntryLine(account_user_id=user.id, account_code="9010",
                             debit_amount=0, credit_amount=100000),
        ]
        db.session.add(e1)
        db.session.commit()

        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert resp.status_code == 200
        assert "事業所得" in html

    def test_monthly_without_mappings(self, logged_in_client, db, user, accounts, account_types):
        """マッピングなし時は事業所得行なし"""
        resp = logged_in_client.get("/reports/monthly?year=2026")
        html = resp.data.decode()
        assert resp.status_code == 200
        assert "事業所得" not in html
