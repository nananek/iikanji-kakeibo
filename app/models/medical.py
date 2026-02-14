from datetime import datetime, timezone

from app.extensions import db


class MedicalExpense(db.Model):
    """医療費明細（確定申告の医療費控除用）"""

    __tablename__ = "medical_expenses"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    journal_entry_id = db.Column(
        db.Integer,
        db.ForeignKey("journal_entries.id", ondelete="CASCADE"),
        nullable=True,
    )
    date = db.Column(db.Date, nullable=False)
    patient_name = db.Column(db.String(100), nullable=False, default="")
    hospital_name = db.Column(db.String(200), nullable=False, default="")
    treatment_description = db.Column(db.String(255), nullable=False, default="")
    provider_type = db.Column(db.String(20), nullable=True)  # hospital/pharmacy/nursing/other
    amount_paid = db.Column(db.Integer, nullable=False)
    insurance_reimbursement = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    journal_entry = db.relationship(
        "JournalEntry", backref="medical_expense", passive_deletes=True
    )

    @property
    def net_amount(self):
        """自己負担額"""
        return self.amount_paid - self.insurance_reimbursement

    def __repr__(self):
        return f"<MedicalExpense {self.date} {self.hospital_name} ¥{self.amount_paid}>"
