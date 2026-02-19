"""tax.py のテスト — 確定申告集計"""

from datetime import date

import pytest

from app.services.tax import get_tax_summary
from tests.conftest import make_journal


class TestGetTaxSummary:
    def test_life_insurance(self, db, user, accounts):
        """生命保険料控除が集計される"""
        # 7010 生命保険料を追加（conftest の accounts にないので作成）
        from app.models.account import Account, AccountType
        expense_type = AccountType.query.filter_by(code="expense").first()
        ins = Account(
            user_id=user.id, account_type_id=expense_type.id,
            code="7010", name="生命保険料", tax_category="life_insurance",
            is_active=True, display_order=200,
        )
        db.session.add(ins)
        db.session.commit()

        make_journal(db, user.id, ins.id, accounts["1010"].id,
                     88370, entry_date=date(2026, 12, 24),
                     description="第一生命保険株式会社")
        summary = get_tax_summary(user.id, 2026)
        assert "life_insurance" in summary
        assert summary["life_insurance"]["total"] == 88370

    def test_closing_entries_excluded(self, db, user, accounts):
        """損益振替仕訳は集計から除外される"""
        from app.models.account import Account, AccountType
        expense_type = AccountType.query.filter_by(code="expense").first()
        ins = Account(
            user_id=user.id, account_type_id=expense_type.id,
            code="7010", name="生命保険料", tax_category="life_insurance",
            is_active=True, display_order=200,
        )
        db.session.add(ins)
        db.session.commit()

        # 通常仕訳
        make_journal(db, user.id, ins.id, accounts["1010"].id,
                     88370, entry_date=date(2026, 12, 24))
        # 損益振替仕訳（逆仕訳）
        make_journal(db, user.id, accounts["3010"].id, ins.id,
                     88370, entry_date=date(2026, 12, 31),
                     source="closing", fiscal_period=16)

        summary = get_tax_summary(user.id, 2026)
        assert "life_insurance" in summary
        assert summary["life_insurance"]["total"] == 88370

    def test_empty(self, db, user, accounts):
        """控除対象の仕訳がない場合は空"""
        summary = get_tax_summary(user.id, 2026)
        assert summary == {}

    def test_social_insurance(self, db, user, accounts):
        """社会保険料控除の集計"""
        from app.models.account import Account, AccountType
        expense_type = AccountType.query.filter_by(code="expense").first()
        kenpo = Account(
            user_id=user.id, account_type_id=expense_type.id,
            code="6020", name="健康保険料", tax_category="social_insurance",
            is_active=True, display_order=201,
        )
        nenkin = Account(
            user_id=user.id, account_type_id=expense_type.id,
            code="6030", name="厚生年金保険料", tax_category="social_insurance",
            is_active=True, display_order=202,
        )
        db.session.add_all([kenpo, nenkin])
        db.session.commit()

        make_journal(db, user.id, kenpo.id, accounts["1020"].id,
                     50000, entry_date=date(2026, 6, 25))
        make_journal(db, user.id, nenkin.id, accounts["1020"].id,
                     90000, entry_date=date(2026, 6, 25))

        summary = get_tax_summary(user.id, 2026)
        assert "social_insurance" in summary
        assert summary["social_insurance"]["total"] == 140000
        assert len(summary["social_insurance"]["accounts"]) == 2
