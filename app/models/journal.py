from datetime import datetime, timezone

from app.extensions import db


class JournalEntry(db.Model):
    """仕訳伝票"""

    __tablename__ = "journal_entries"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    entry_number = db.Column(db.Integer, nullable=False)
    batch_id = db.Column(db.String(36), nullable=True, index=True)
    # E3-F PR-D-6-5 (055): 平文列 date / description / source / fiscal_period は
    # DROP 済。本体は encrypted_blob のみに格納する。
    # Phase E3: クライアント側 MK で AES-256-GCM 暗号化されたレコード本体
    # (date / description / source / fiscal_period の暗号化版)。closing 仕訳は
    # サーバが暗号化できないため空 blob (b"") + ゼロ IV のセンチネル。
    # default=b"" はテスト等で未設定時のフォールバック (実 API は必須)。
    encrypted_blob = db.Column(db.LargeBinary, nullable=False, default=b"")
    blob_iv = db.Column(db.LargeBinary, nullable=False, default=b"")  # AES-GCM IV (12B)
    # date 暗号化後の年度フィルタ用 (平文)。漏れる情報は「何年度に仕訳が
    # 何件あるか」のみ。
    fiscal_year = db.Column(db.SmallInteger, nullable=True)
    # E3-F: DROP 済の source / fiscal_period の平文代替カラム。
    # is_closing は旧 source == 'closing' (損益振替の自動生成仕訳) の判定、
    # fiscal_month は旧 fiscal_period と同じ値域
    # (0=期首振戻, 1-12=通常月, 13-15=決算月, 16=損益振替)。
    is_closing = db.Column(db.Boolean, nullable=False, default=False)
    fiscal_month = db.Column(db.SmallInteger, nullable=True)
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

    # 論理削除されていない vouchers のみを SQL レベルでフィルタして返す
    # (PR #94 review Finding 1 反映 — 旧実装は Python フィルタだったが
    # 削除済が大量蓄積した場合の負荷を避けるため SQL レベルに移行)。
    # `vouchers` backref は引き続き全件を返すので、`log_voucher_orphan` /
    # `api_voucher_logs` 等の証跡参照用途はそちらを使うこと。
    active_vouchers = db.relationship(
        "Voucher",
        primaryjoin=(
            "and_("
            "JournalEntry.id == Voucher.journal_entry_id, "
            "Voucher.deleted_at.is_(None)"
            ")"
        ),
        viewonly=True,
        order_by="Voucher.id",
        lazy="select",
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
        # E3-F PR-D-6-5-pre1: 平文 date は参照しない (D-6-5 で DROP)。
        return (
            f"<JournalEntry #{self.entry_number} "
            f"{self.fiscal_year}-{self.fiscal_month}>"
        )


class JournalEntryLine(db.Model):
    """仕訳明細行"""

    __tablename__ = "journal_entry_lines"

    id = db.Column(db.Integer, primary_key=True)
    journal_entry_id = db.Column(
        db.Integer, db.ForeignKey("journal_entries.id"), nullable=False
    )
    account_user_id = db.Column(db.Integer, nullable=False)
    # #338 item5 (Phase 5a-b, migration 067): account_code / debit_amount /
    # credit_amount は read 側を全て client 化済 (item4 応答平文除去 + Phase R
    # レポート集計)。Phase 5b で write を停止し、新規行はこれらを NULL で書く
    # (本体は encrypted_blob のみ)。default=0 を外したのは None を明示的に NULL の
    # まま保存するため (default があると None→0 に化けてしまう)。item8 (068) で
    # FK 撤去のうえ物理 DROP する。
    account_code = db.Column(db.String(10), nullable=True)
    debit_amount = db.Column(db.Numeric(12, 0), nullable=True)
    credit_amount = db.Column(db.Numeric(12, 0), nullable=True)
    # E3-F PR-D-6-5 (055): 平文 description は DROP 済。本体は encrypted_blob のみ。
    # Phase E3: クライアント暗号化された account_code / debit / credit / description
    # の本体。AAD には user_id のみを含む (Option B)。closing 仕訳行は空 blob
    # センチネル。default=b"" は未設定時のフォールバック。
    encrypted_blob = db.Column(db.LargeBinary, nullable=False, default=b"")
    blob_iv = db.Column(db.LargeBinary, nullable=False, default=b"")  # AES-GCM IV (12B)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["account_user_id", "account_code"],
            ["accounts.user_id", "accounts.code"],
            name="fk_jel_account",
        ),
    )

    def __repr__(self):
        return f"<JournalEntryLine {self.account_code} D:{self.debit_amount} C:{self.credit_amount}>"
