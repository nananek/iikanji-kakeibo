from datetime import datetime, timezone

from app.extensions import db


class JournalEntry(db.Model):
    """仕訳伝票"""

    __tablename__ = "journal_entries"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    entry_number = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(255), nullable=False, default="")
    source = db.Column(db.String(20), nullable=False, default="journal")
    batch_id = db.Column(db.String(36), nullable=True, index=True)
    fiscal_period = db.Column(db.Integer, nullable=True)  # 0=期首振戻, 1-12=通常月, 13-15=決算月1-3
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    lines = db.relationship(
        "JournalEntryLine",
        backref="journal_entry",
        lazy="select",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "entry_number", name="uq_user_entry_number"
        ),
    )

    @property
    def total_debit(self):
        return sum(line.debit_amount for line in self.lines)

    @property
    def total_credit(self):
        return sum(line.credit_amount for line in self.lines)

    @property
    def is_balanced(self):
        return self.total_debit == self.total_credit

    def __repr__(self):
        return f"<JournalEntry #{self.entry_number} {self.date}>"


class JournalEntryLine(db.Model):
    """仕訳明細行"""

    __tablename__ = "journal_entry_lines"

    id = db.Column(db.Integer, primary_key=True)
    journal_entry_id = db.Column(
        db.Integer, db.ForeignKey("journal_entries.id"), nullable=False
    )
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    debit_amount = db.Column(db.Numeric(12, 0), nullable=False, default=0)
    credit_amount = db.Column(db.Numeric(12, 0), nullable=False, default=0)
    description = db.Column(db.String(255), default="")

    def __repr__(self):
        return f"<JournalEntryLine {self.account_id} D:{self.debit_amount} C:{self.credit_amount}>"
