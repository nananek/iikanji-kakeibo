from app.extensions import db


class AccountType(db.Model):
    """勘定科目区分（資産・負債・純資産・収益・費用）"""

    __tablename__ = "account_types"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)  # 資産, 負債, ...
    code = db.Column(db.String(10), unique=True, nullable=False)  # asset, liability, ...
    normal_balance = db.Column(db.String(10), nullable=False)  # debit or credit
    display_order = db.Column(db.Integer, nullable=False, default=0)

    accounts = db.relationship("Account", backref="account_type", lazy="dynamic")

    def __repr__(self):
        return f"<AccountType {self.name}>"


class Account(db.Model):
    """勘定科目"""

    __tablename__ = "accounts"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    code = db.Column(db.String(10), primary_key=True)
    account_type_id = db.Column(
        db.Integer, db.ForeignKey("account_types.id"), nullable=False
    )
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255), default="")
    tax_category = db.Column(db.String(50), nullable=True)
    cost_type = db.Column(db.String(10), nullable=True)  # 'fixed' / 'variable' / 'occasional'
    system_role = db.Column(db.String(30), nullable=True)  # 'proprietor' / 'retained_earnings'
    is_system = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    deactivated_year = db.Column(db.Integer, nullable=True)
    display_order = db.Column(db.Integer, nullable=False, default=0)

    journal_lines = db.relationship(
        "JournalEntryLine", backref="account", lazy="dynamic"
    )

    def __repr__(self):
        return f"<Account {self.code} {self.name}>"
