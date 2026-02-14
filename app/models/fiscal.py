from app.extensions import db


class FiscalClose(db.Model):
    """月次確定の管理"""

    __tablename__ = "fiscal_closes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    closed_period = db.Column(db.Integer, nullable=False, default=-1)

    __table_args__ = (
        db.UniqueConstraint("user_id", "year", name="uq_fiscal_close_user_year"),
    )

    def __repr__(self):
        return f"<FiscalClose {self.year} closed_period={self.closed_period}>"
