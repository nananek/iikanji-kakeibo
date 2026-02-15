from app.extensions import db


class BalanceCache(db.Model):
    """確定済み期間の残高キャッシュ（当年内累計）"""

    __tablename__ = "balance_caches"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    period = db.Column(db.Integer, nullable=False)  # 0-16
    cumulative_debit = db.Column(db.Numeric(15, 0), nullable=False, default=0)
    cumulative_credit = db.Column(db.Numeric(15, 0), nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "account_id", "year", "period",
            name="uq_balance_cache",
        ),
        db.Index("ix_balance_cache_user_year", "user_id", "year"),
    )

    def __repr__(self):
        return f"<BalanceCache account={self.account_id} {self.year}/p{self.period}>"
