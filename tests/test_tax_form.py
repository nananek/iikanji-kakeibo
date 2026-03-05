"""青色申告決算書マッピングのテスト"""
import pytest

from app.models.tax_form import TaxFormField, TaxFormMapping
from app.models.account import Account
from app.services.tax_form import (
    get_form_fields,
    get_mappable_fields,
    get_user_mappings,
    get_account_mapping,
    set_mapping,
    remove_mapping,
    bulk_create_accounts,
    save_mappings,
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
