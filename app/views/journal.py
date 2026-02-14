import json
from datetime import date

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.forms.journal import JournalForm
from app.services.accounting import create_journal_entry, get_next_entry_number
from app.services.fiscal import check_entry_modifiable, check_period_open_for_new, get_effective_period
from app.views.helpers import get_grouped_accounts

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
    grouped_accounts = get_grouped_accounts(current_user.id)

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
                grouped_accounts=grouped_accounts,
                is_edit=False,
            )

        if not lines_data:
            flash("仕訳明細を1行以上入力してください。", "danger")
            return render_template(
                "journal/form.html",
                form=form,
                grouped_accounts=grouped_accounts,
                is_edit=False,
            )

        # 計上期間の決定
        fiscal_period = None
        if form.fiscal_period.data:
            fiscal_period = int(form.fiscal_period.data)
        period = fiscal_period if fiscal_period is not None else form.date.data.month

        # 確定済み期間チェック
        err = check_period_open_for_new(current_user.id, form.date.data.year, period)
        if err:
            flash(err, "danger")
            return render_template(
                "journal/form.html",
                form=form,
                grouped_accounts=grouped_accounts,
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
                fiscal_period=fiscal_period,
            )
            flash(f"伝票 #{entry.entry_number} を登録しました。", "success")
            return redirect(url_for("journal.index"))
        except ValueError as e:
            flash(str(e), "danger")

    return render_template(
        "journal/form.html",
        form=form,
        grouped_accounts=grouped_accounts,
        is_edit=False,
    )


@bp.route("/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def edit(entry_id):
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=current_user.id
    ).first_or_404()

    # 確定済み期間チェック
    err = check_entry_modifiable(current_user.id, entry)
    if err:
        flash(err, "danger")
        return redirect(url_for("journal.index"))

    form = JournalForm()
    grouped_accounts = get_grouped_accounts(current_user.id)

    if request.method == "POST" and form.validate_on_submit():
        lines_json = request.form.get("lines_json", "[]")
        try:
            lines_data = json.loads(lines_json)
        except json.JSONDecodeError:
            flash("明細データが不正です。", "danger")
            return render_template(
                "journal/form.html",
                form=form,
                grouped_accounts=grouped_accounts,
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
                grouped_accounts=grouped_accounts,
                is_edit=True,
                entry=entry,
            )

        # 計上期間の決定
        fiscal_period = None
        if form.fiscal_period.data:
            fiscal_period = int(form.fiscal_period.data)

        entry.date = form.date.data
        entry.description = form.description.data
        entry.fiscal_period = fiscal_period

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
        form.fiscal_period.data = str(entry.fiscal_period) if entry.fiscal_period is not None else ""

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
        grouped_accounts=grouped_accounts,
        is_edit=True,
        entry=entry,
        existing_lines=json.dumps(existing_lines),
    )


@bp.route("/<int:entry_id>/json")
@login_required
def get_json(entry_id):
    """仕訳データをJSON形式で返す（モーダル編集用）"""
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=current_user.id
    ).first_or_404()

    return jsonify({
        "id": entry.id,
        "date": entry.date.isoformat(),
        "description": entry.description,
        "entry_number": entry.entry_number,
        "fiscal_period": entry.fiscal_period,
        "lines": [
            {
                "account_id": line.account_id,
                "debit_amount": int(line.debit_amount),
                "credit_amount": int(line.credit_amount),
                "description": line.description or "",
            }
            for line in entry.lines
        ],
    })


@bp.route("/<int:entry_id>/edit-api", methods=["POST"])
@login_required
def edit_api(entry_id):
    """仕訳をJSON APIで更新する（モーダル編集用）"""
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=current_user.id
    ).first_or_404()

    # 確定済み期間チェック
    err = check_entry_modifiable(current_user.id, entry)
    if err:
        return jsonify({"error": err}), 400

    data = request.get_json()
    if not data:
        return jsonify({"error": "リクエストが不正です。"}), 400

    entry_date = data.get("date")
    description = data.get("description", "").strip()
    lines_data = data.get("lines", [])

    if not entry_date or not description:
        return jsonify({"error": "日付と摘要は必須です。"}), 400

    if not lines_data:
        return jsonify({"error": "仕訳明細を1行以上入力してください。"}), 400

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
        return jsonify({
            "error": f"貸借が一致しません（借方: {total_debit:,}, 貸方: {total_credit:,}）"
        }), 400

    # 計上期間の決定
    raw_period = data.get("fiscal_period")
    fiscal_period = int(raw_period) if raw_period not in (None, "") else None

    entry.date = date.fromisoformat(entry_date)
    entry.description = description
    entry.fiscal_period = fiscal_period

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
    return jsonify({"ok": True, "entry_number": entry.entry_number})


@bp.route("/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete(entry_id):
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=current_user.id
    ).first_or_404()

    # 確定済み期間チェック
    err = check_entry_modifiable(current_user.id, entry)
    if err:
        flash(err, "danger")
        return redirect(url_for("journal.index"))

    num = entry.entry_number
    db.session.delete(entry)
    db.session.commit()
    flash(f"伝票 #{num} を削除しました。", "success")
    return redirect(url_for("journal.index"))


@bp.route("/bulk-delete", methods=["POST"])
@login_required
def bulk_delete():
    """仕訳の一括削除"""
    entry_ids = request.form.getlist("entry_ids", type=int)
    redirect_url = request.form.get("redirect_url") or url_for("journal.index")

    if not entry_ids:
        flash("削除する仕訳が選択されていません。", "warning")
        return redirect(redirect_url)

    entries = JournalEntry.query.filter(
        JournalEntry.id.in_(entry_ids),
        JournalEntry.user_id == current_user.id,
    ).all()

    # 確定済み期間チェック
    locked = []
    deletable = []
    for entry in entries:
        err = check_entry_modifiable(current_user.id, entry)
        if err:
            locked.append(entry)
        else:
            deletable.append(entry)

    if locked:
        flash(f"{len(locked)}件の仕訳は確定済み期間のため削除できませんでした。", "warning")

    count = len(deletable)
    for entry in deletable:
        db.session.delete(entry)
    db.session.commit()
    flash(f"{count}件の仕訳を削除しました。", "success")
    return redirect(redirect_url)


SOURCE_LABELS = {
    "cashbook": "出納帳 / CSV / Web取込",
    "journal": "仕訳帳",
    "ai_receipt": "AI証憑仕訳",
    "closing": "損益振替（自動生成）",
}


@bp.route("/batches")
@login_required
def batches():
    """インポート履歴"""
    batch_list = (
        db.session.query(
            JournalEntry.batch_id,
            JournalEntry.source,
            func.count(JournalEntry.id).label("count"),
            func.min(JournalEntry.date).label("date_from"),
            func.max(JournalEntry.date).label("date_to"),
            func.min(JournalEntry.created_at).label("imported_at"),
        )
        .filter(
            JournalEntry.user_id == current_user.id,
            JournalEntry.batch_id.isnot(None),
        )
        .group_by(JournalEntry.batch_id, JournalEntry.source)
        .order_by(func.min(JournalEntry.created_at).desc())
        .all()
    )

    return render_template(
        "journal/batches.html",
        batches=batch_list,
        source_labels=SOURCE_LABELS,
    )


@bp.route("/batches/<batch_id>/delete", methods=["POST"])
@login_required
def delete_batch(batch_id):
    """インポートバッチの一括削除"""
    entries = JournalEntry.query.filter_by(
        user_id=current_user.id, batch_id=batch_id
    ).all()

    if not entries:
        flash("該当するバッチが見つかりません。", "warning")
        return redirect(url_for("journal.batches"))

    # 確定済み期間チェック
    locked = []
    deletable = []
    for entry in entries:
        err = check_entry_modifiable(current_user.id, entry)
        if err:
            locked.append(entry)
        else:
            deletable.append(entry)

    if locked:
        flash(f"{len(locked)}件の仕訳は確定済み期間のため削除できませんでした。", "warning")

    count = len(deletable)
    for entry in deletable:
        db.session.delete(entry)
    db.session.commit()
    flash(f"{count}件の仕訳を削除しました。", "success")
    return redirect(url_for("journal.batches"))
