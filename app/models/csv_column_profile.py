"""CSV列マッピングプロファイル"""

from datetime import datetime, timezone

from app.extensions import db


class CsvColumnProfile(db.Model):
    """口座ごとのCSV列マッピングプロファイル"""

    __tablename__ = "csv_column_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    account_code = db.Column(db.String(10), nullable=False)

    date_col = db.Column(db.Integer, nullable=False)
    desc_col = db.Column(db.Integer, nullable=False)
    deposit_col = db.Column(db.Integer, nullable=True)
    withdrawal_col = db.Column(db.Integer, nullable=True)
    amount_col = db.Column(db.Integer, nullable=True)
    date_format = db.Column(db.String(30), nullable=False, default="%Y/%m/%d")
    amount_mode = db.Column(db.String(10), nullable=False, default="separate")

    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["user_id", "account_code"],
            ["accounts.user_id", "accounts.code"],
            name="fk_csv_profile_account",
        ),
        db.UniqueConstraint(
            "user_id", "account_code",
            name="uq_csv_profile_user_account",
        ),
    )

    def __repr__(self):
        return f"<CsvColumnProfile user={self.user_id} account={self.account_code}>"

    def to_mapping_dict(self):
        """テンプレート向けのマッピング辞書を返す"""
        return {
            "date_col": self.date_col,
            "desc_col": self.desc_col,
            "deposit_col": self.deposit_col,
            "withdrawal_col": self.withdrawal_col,
            "amount_col": self.amount_col,
            "date_format": self.date_format,
            "amount_mode": self.amount_mode,
        }
