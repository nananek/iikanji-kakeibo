from app.extensions import db


class TaxFormField(db.Model):
    """青色申告決算書の欄定義（マスタデータ）"""

    __tablename__ = "tax_form_fields"

    id = db.Column(db.Integer, primary_key=True)
    form_type = db.Column(db.String(20), nullable=False)  # general / real_estate / agriculture
    page = db.Column(db.Integer, nullable=False)  # 1-4
    section = db.Column(db.String(30), nullable=False)  # revenue, cost_of_sales, expenses, bs_assets, bs_liabilities ...
    row_code = db.Column(db.String(10), nullable=False)  # 欄番号: "1", "2", ... or "ア", "イ", ...
    name = db.Column(db.String(100), nullable=False)  # 欄名
    account_type_code = db.Column(db.String(10), nullable=False)  # asset/liability/equity/revenue/expense
    suggested_code = db.Column(db.String(10), nullable=True)  # 一括作成時の推奨科目コード (9xxx)
    is_subtotal = db.Column(db.Boolean, nullable=False, default=False)
    is_user_defined = db.Column(db.Boolean, nullable=False, default=False)  # ユーザー自由記入欄
    display_order = db.Column(db.Integer, nullable=False, default=0)

    mappings = db.relationship("TaxFormMapping", backref="field", lazy="dynamic")

    def __repr__(self):
        return f"<TaxFormField {self.form_type}:{self.row_code} {self.name}>"


class TaxFormMapping(db.Model):
    """ユーザーの勘定科目 → 決算書欄のマッピング"""

    __tablename__ = "tax_form_mappings"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["user_id", "account_code"],
            ["accounts.user_id", "accounts.code"],
            name="fk_tax_mapping_account",
        ),
        db.UniqueConstraint(
            "user_id", "account_code", "field_id",
            name="uq_tax_mapping_user_account_field",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    account_code = db.Column(db.String(10), nullable=False)
    field_id = db.Column(db.Integer, db.ForeignKey("tax_form_fields.id"), nullable=False)
