import json
from datetime import date

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.forms.journal import JournalForm
from app.services.accounting import create_journal_entry, get_next_entry_number

bp = Blueprint("journal", __name__, url_prefix="/journal")


@bp.route("/")
@login_required
def index():
    page = request.args.get("page", 1, type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    search = request.args.get("search", "")

    query = (
        JournalEntry.query
        .filter_by(user_id=current_user.id)
        .order_by(JournalEntry.date.desc(), JournalEntry.entry_number.desc())
    )

    if date_from:
        query = query.filter(JournalEntry.date >= date_from)
    if date_to:
        query = query.filter(JournalEntry.date <= date_to)
    if search:
        query = query.filter(JournalEntry.description.ilike(f"%{search}%"))

    entries = query.paginate(page=page, per_page=20, error_out=False)
    return render_template(
        "journal/index.html",
        entries=entries,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    form = JournalForm()
    accounts = (
        Account.query
        .filter_by(user_id=current_user.id, is_active=True)
        .order_by(Account.code)
        .all()
    )
    account_choices = [(a.id, f"{a.code} {a.name}") for a in accounts]

    if not form.date.data:
        form.date.data = date.today()

    if request.method == "POST" and form.validate_on_submit():
        lines_json = request.form.get("lines_json", "[]")
        try:
            lines_data = json.loads(lines_json)
        except json.JSONDecodeError:
            flash("明細データが不正です。", "danger")
            return render_template(
                "journal/form.html",
                form=form,
                account_choices=account_choices,
                is_edit=False,
            )

        if not lines_data:
            flash("仕訳明細を1行以上入力してください。", "danger")
            return render_template(
                "journal/form.html",
                form=form,
                account_choices=account_choices,
                is_edit=False,
            )

        parsed = []
        for line in lines_data:
            parsed.append({
                "account_id": int(line["account_id"]),
                "debit_amount": int(line.get("debit_amount", 0) or 0),
                "credit_amount": int(line.get("credit_amount", 0) or 0),
                "description": line.get("description", ""),
            })

        try:
            entry = create_journal_entry(
                user_id=current_user.id,
                date=form.date.data,
                description=form.description.data,
                lines_data=parsed,
            )
            flash(f"伝票 #{entry.entry_number} を登録しました。", "success")
            return redirect(url_for("journal.index"))
        except ValueError as e:
            flash(str(e), "danger")

    return render_template(
        "journal/form.html",
        form=form,
        account_choices=account_choices,
        is_edit=False,
    )


@bp.route("/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit(entry_id):
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=current_user.id
    ).first_or_404()

    form = JournalForm()
    accounts = (
        Account.query
        .filter_by(user_id=current_user.id, is_active=True)
        .order_by(Account.code)
        .all()
    )
    account_choices = [(a.id, f"{a.code} {a.name}") for a in accounts]

    if request.method == "POST" and form.validate_on_submit():
        lines_json = request.form.get("lines_json", "[]")
        try:
            lines_data = json.loads(lines_json)
        except json.JSONDecodeError:
            flash("明細データが不正です。", "danger")
            return render_template(
                "journal/form.html",
                form=form,
                account_choices=account_choices,
                is_edit=True,
                entry=entry,
            )

        parsed = []
        for line in lines_data:
            parsed.append({
                "account_id": int(line["account_id"]),
                "debit_amount": int(line.get("debit_amount", 0) or 0),
                "credit_amount": int(line.get("credit_amount", 0) or 0),
                "description": line.get("description", ""),
            })

        total_debit = sum(l["debit_amount"] for l in parsed)
        total_credit = sum(l["credit_amount"] for l in parsed)
        if total_debit != total_credit:
            flash(f"貸借が一致しません（借方: {total_debit:,}, 貸方: {total_credit:,}）", "danger")
            return render_template(
                "journal/form.html",
                form=form,
                account_choices=account_choices,
                is_edit=True,
                entry=entry,
            )

        entry.date = form.date.data
        entry.description = form.description.data

        # 既存明細を削除して再作成
        for line in entry.lines:
            db.session.delete(line)
        db.session.flush()

        for line_data in parsed:
            db.session.add(JournalEntryLine(
                journal_entry_id=entry.id,
                account_id=line_data["account_id"],
                debit_amount=line_data["debit_amount"],
                credit_amount=line_data["credit_amount"],
                description=line_data.get("description", ""),
            ))

        db.session.commit()
        flash(f"伝票 #{entry.entry_number} を更新しました。", "success")
        return redirect(url_for("journal.index"))

    if request.method == "GET":
        form.date.data = entry.date
        form.description.data = entry.description

    existing_lines = [
        {
            "account_id": line.account_id,
            "debit_amount": int(line.debit_amount),
            "credit_amount": int(line.credit_amount),
            "description": line.description or "",
        }
        for line in entry.lines
    ]

    return render_template(
        "journal/form.html",
        form=form,
        account_choices=account_choices,
        is_edit=True,
        entry=entry,
        existing_lines=json.dumps(existing_lines),
    )


@bp.route("/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete(entry_id):
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=current_user.id
    ).first_or_404()

    num = entry.entry_number
    db.session.delete(entry)
    db.session.commit()
    flash(f"伝票 #{num} を削除しました。", "success")
    return redirect(url_for("journal.index"))
