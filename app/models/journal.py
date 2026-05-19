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
    fiscal_period = db.Column(db.Integer, nullable=True)  # 0=期首振戻, 1-12=通常月, 13-15=決算月1-3, 16=損益振替
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
    def active_vouchers(self):
        """論理削除されていない vouchers のみを返す (Phase 5 #70 電帳法証跡)。

        Voucher 削除を物理 → 論理 (`deleted_at` セット) に変更したため、
        `self.vouchers` backref は削除済も含む全件を返す。UI / API では
        削除済を含めると「証憑あり」と誤判定されたり、ai_journal.voucher_image
        が 404 を返す不整合が発生するため、本プロパティ経由でフィルタする。

        全件 (削除済含む) が必要な log_voucher_orphan / api_voucher_logs
        などは `self.vouchers` を直接使うこと。
        """
        return [v for v in self.vouchers if v.deleted_at is None]

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
    account_user_id = db.Column(db.Integer, nullable=False)
    account_code = db.Column(db.String(10), nullable=False)
    debit_amount = db.Column(db.Numeric(12, 0), nullable=False, default=0)
    credit_amount = db.Column(db.Numeric(12, 0), nullable=False, default=0)
    description = db.Column(db.String(255), default="")

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["account_user_id", "account_code"],
            ["accounts.user_id", "accounts.code"],
            name="fk_jel_account",
        ),
    )

    def __repr__(self):
        return f"<JournalEntryLine {self.account_code} D:{self.debit_amount} C:{self.credit_amount}>"
