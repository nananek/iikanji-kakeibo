from app.extensions import db


class BalanceCache(db.Model):
    """確定済み期間の残高キャッシュ（当年内累計）"""

    __tablename__ = "balance_caches"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    account_code = db.Column(db.String(10), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    period = db.Column(db.Integer, nullable=False)  # 0-16
    cumulative_debit = db.Column(db.Numeric(15, 0), nullable=False, default=0)
    cumulative_credit = db.Column(db.Numeric(15, 0), nullable=False, default=0)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["user_id", "account_code"],
            ["accounts.user_id", "accounts.code"],
            name="fk_bc_account",
        ),
        db.UniqueConstraint(
            "user_id", "account_code", "year", "period",
            name="uq_balance_cache",
        ),
        db.Index("ix_balance_cache_user_year", "user_id", "year"),
    )

    def __repr__(self):
        return f"<BalanceCache account={self.account_code} {self.year}/p{self.period}>"


class BalanceCacheBlob(db.Model):
    """確定済み期間の残高キャッシュ (クライアント暗号化済 blob)。

    Phase E3-E-1: BalanceCache (平文) と並存する dual storage。
    クライアントが自分の MK で AES-GCM 暗号化した
    ``{account_code: [debit, credit], ...}`` の JSON を blob として保存する。
    1 (user_id, year, period) で 1 行 = 1 blob。

    AAD はクライアント側で
    ``b"bcb\\0" + uint64_be(user_id) + b"\\0" + uint64_be(year*100+period)``
    として構築する (E3 record 暗号化と同パターン)。

    E3-F で BalanceCache (平文) を撤去予定。
    """

    __tablename__ = "balance_cache_blobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    period = db.Column(db.Integer, nullable=False)  # 0-16
    encrypted_blob = db.Column(db.LargeBinary, nullable=False)
    blob_iv = db.Column(db.LargeBinary(12), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.current_timestamp(),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "year", "period",
            name="uq_balance_cache_blobs",
        ),
        db.Index(
            "ix_balance_cache_blobs_user_year",
            "user_id", "year",
        ),
    )

    def __repr__(self):
        return (
            f"<BalanceCacheBlob user={self.user_id} "
            f"{self.year}/p{self.period}>"
        )
