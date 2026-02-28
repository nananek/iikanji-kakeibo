"""証憑一覧ビュー — 電帳法検索要件対応"""

from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.voucher import Voucher
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.audit import get_effective_user_id

bp = Blueprint("vouchers", __name__, url_prefix="/vouchers")


@bp.route("/")
@login_required
def index():
    """証憑一覧（日付・金額・摘要で検索可能）"""
    user_id = get_effective_user_id()
    page = request.args.get("page", 1, type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    amount_from = request.args.get("amount_from", "")
    amount_to = request.args.get("amount_to", "")
    search = request.args.get("search", "")

    query = (
        Voucher.query
        .outerjoin(JournalEntry, Voucher.journal_entry_id == JournalEntry.id)
        .options(joinedload(Voucher.journal_entry))
        .filter(Voucher.user_id == user_id)
    )

    # 日付フィルタ
    if date_from:
        query = query.filter(
            db.or_(
                JournalEntry.date >= date_from,
                db.and_(
                    Voucher.journal_entry_id.is_(None),
                    func.date(Voucher.uploaded_at) >= date_from,
                ),
            )
        )
    if date_to:
        query = query.filter(
            db.or_(
                JournalEntry.date <= date_to,
                db.and_(
                    Voucher.journal_entry_id.is_(None),
                    func.date(Voucher.uploaded_at) <= date_to,
                ),
            )
        )

    # 摘要フィルタ
    if search:
        query = query.filter(JournalEntry.description.ilike(f"%{search}%"))

    # 金額フィルタ
    if amount_from or amount_to:
        amount_subq = (
            db.session.query(
                JournalEntryLine.journal_entry_id,
                func.sum(JournalEntryLine.debit_amount).label("total"),
            )
            .group_by(JournalEntryLine.journal_entry_id)
            .subquery()
        )
        query = query.outerjoin(
            amount_subq,
            JournalEntry.id == amount_subq.c.journal_entry_id,
        )
        if amount_from:
            query = query.filter(amount_subq.c.total >= int(amount_from))
        if amount_to:
            query = query.filter(amount_subq.c.total <= int(amount_to))

    query = query.order_by(
        func.coalesce(JournalEntry.date, func.date(Voucher.uploaded_at)).desc(),
        Voucher.id.desc(),
    )

    vouchers = query.paginate(page=page, per_page=20, error_out=False)

    return render_template(
        "vouchers/index.html",
        vouchers=vouchers,
        date_from=date_from,
        date_to=date_to,
        amount_from=amount_from,
        amount_to=amount_to,
        search=search,
    )
