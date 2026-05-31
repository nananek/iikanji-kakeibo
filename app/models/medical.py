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
    # E3-F PR-D-6-5 (055): 平文列 (date / patient_name / hospital_name /
    # treatment_description / provider_type / amount_paid /
    # insurance_reimbursement) は DROP 済。本体は encrypted_blob のみ。
    # Phase E3: クライアント暗号化された全フィールド本体。AAD には user_id を
    # 含む (Option B)。default=b"" は未設定時のフォールバック (実 API は必須)。
    encrypted_blob = db.Column(db.LargeBinary, nullable=False, default=b"")
    blob_iv = db.Column(db.LargeBinary, nullable=False, default=b"")  # AES-GCM IV (12B)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    journal_entry = db.relationship(
        "JournalEntry", backref="medical_expense", passive_deletes=True
    )

    # E3-F PR-D-6-5-pre1: net_amount プロパティ (amount_paid - insurance_reimbursement)
    # は撤去。サーバ未使用で、平文列 amount_paid は D-6-5 で DROP 予定 (自己負担額の
    # 算出はクライアントが復号 body から行う)。

    def __repr__(self):
        return (
            f"<MedicalExpense id={self.id} "
            f"journal_entry_id={self.journal_entry_id}>"
        )
