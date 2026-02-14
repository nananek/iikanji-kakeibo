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
from app.services.fiscal import check_entry_modifiable, check_period_open_for_new, get_effective_period, adjust_date_for_fiscal_period
from app.services.audit import (
    get_effective_user_id, get_allowed_account_ids, get_submitted_account_ids,
    is_entry_locked_for_owner, is_entry_locked_for_auditor,
    is_acting_as_auditor, get_permission_level,
    get_proprietor_account_id, mask_account_name,
)
from app.views.helpers import get_grouped_accounts

bp = Blueprint("journal", __name__, url_prefix="/journal")


@bp.route("/")
@login_required
def index():
    page = request.args.get("page", 1, type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    search = request.args.get("search", "")

    allowed_ids = get_allowed_account_ids()

    query = (
        JournalEntry.query
        .filter_by(user_id=get_effective_user_id())
        .order_by(JournalEntry.date.desc(), JournalEntry.entry_number.desc())
    )

    # Lv2: 公開科目を1つも含まない伝票を除外
    if allowed_ids is not None:
        query = query.filter(
            JournalEntry.id.in_(
                db.session.query(JournalEntryLine.journal_entry_id)
                .filter(JournalEntryLine.account_id.in_(allowed_ids))
            )
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
    user_id = get_effective_user_id()
    allowed_ids = get_allowed_account_ids()
    grouped_accounts = get_grouped_accounts(user_id, allowed_ids)

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
        # 特殊期間の日付補正
        form.date.data = adjust_date_for_fiscal_period(form.date.data, fiscal_period)
        period = fiscal_period if fiscal_period is not None else form.date.data.month

        # 確定済み期間チェック
        err = check_period_open_for_new(get_effective_user_id(), form.date.data.year, period)
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

        # 本人側: 提出済みロック科目チェック
        if not is_acting_as_auditor():
            locked_ids = get_submitted_account_ids(user_id)
            if locked_ids:
                used_locked = [p for p in parsed if p["account_id"] in locked_ids]
                if used_locked:
                    flash("提出済みの税務科目を含むため仕訳を作成できません。", "danger")
                    return render_template(
                        "journal/form.html",
                        form=form,
                        grouped_accounts=grouped_accounts,
                        is_edit=False,
                    )

        # Lv2監査者: 非公開科目チェック
        if allowed_ids is not None:
            for p in parsed:
                if p["account_id"] not in allowed_ids:
                    flash("使用できない科目が含まれています。", "danger")
                    return render_template(
                        "journal/form.html",
                        form=form,
                        grouped_accounts=grouped_accounts,
                        is_edit=False,
                    )

        try:
            entry = create_journal_entry(
                user_id=user_id,
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
        id=entry_id, user_id=get_effective_user_id()
    ).first_or_404()

    user_id = get_effective_user_id()
    allowed_ids = get_allowed_account_ids()

    # 伝票ロック: 本人側
    if not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, entry):
        flash("提出済みの税務科目を含む伝票のため変更できません。", "danger")
        return redirect(url_for("journal.index"))

    # 確定済み期間チェック
    err = check_entry_modifiable(get_effective_user_id(), entry)
    if err:
        flash(err, "danger")
        return redirect(url_for("journal.index"))

    form = JournalForm()
    grouped_accounts = get_grouped_accounts(get_effective_user_id(), allowed_ids)
    proprietor_id = get_proprietor_account_id(user_id) if allowed_ids is not None else None

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

        # Lv2: 事業主行をスキップして公開科目のみ受け入れ
        parsed = []
        for line in lines_data:
            aid = int(line["account_id"])
            if allowed_ids is not None and proprietor_id and aid == proprietor_id:
                continue
            parsed.append({
                "account_id": aid,
                "debit_amount": int(line.get("debit_amount", 0) or 0),
                "credit_amount": int(line.get("credit_amount", 0) or 0),
                "description": line.get("description", ""),
            })

        if allowed_ids is not None:
            # Lv2: 非公開行を保持、公開行のみ差し替え
            non_public_debit = sum(
                int(l.debit_amount) for l in entry.lines if l.account_id not in allowed_ids
            )
            non_public_credit = sum(
                int(l.credit_amount) for l in entry.lines if l.account_id not in allowed_ids
            )
            public_debit = sum(l["debit_amount"] for l in parsed)
            public_credit = sum(l["credit_amount"] for l in parsed)
            total_debit = public_debit + non_public_debit
            total_credit = public_credit + non_public_credit
            if total_debit != total_credit:
                flash(f"貸借が一致しません（借方: {total_debit:,}, 貸方: {total_credit:,}）", "danger")
                return render_template(
                    "journal/form.html",
                    form=form,
                    grouped_accounts=grouped_accounts,
                    is_edit=True,
                    entry=entry,
                )

            fiscal_period = None
            if form.fiscal_period.data:
                fiscal_period = int(form.fiscal_period.data)
            form.date.data = adjust_date_for_fiscal_period(form.date.data, fiscal_period)

            # 変更先の期間が確定済みでないかチェック
            new_period = fiscal_period if fiscal_period is not None else form.date.data.month
            err = check_period_open_for_new(user_id, form.date.data.year, new_period)
            if err:
                flash(err, "danger")
                return render_template(
                    "journal/form.html",
                    form=form,
                    grouped_accounts=grouped_accounts,
                    is_edit=True,
                    entry=entry,
                )

            entry.date = form.date.data
            entry.description = form.description.data
            entry.fiscal_period = fiscal_period

            for line in entry.lines:
                if line.account_id in allowed_ids:
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

        # 通常 / Lv3
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

        fiscal_period = None
        if form.fiscal_period.data:
            fiscal_period = int(form.fiscal_period.data)
        form.date.data = adjust_date_for_fiscal_period(form.date.data, fiscal_period)

        # 変更先の期間が確定済みでないかチェック
        new_period = fiscal_period if fiscal_period is not None else form.date.data.month
        err = check_period_open_for_new(user_id, form.date.data.year, new_period)
        if err:
            flash(err, "danger")
            return render_template(
                "journal/form.html",
                form=form,
                grouped_accounts=grouped_accounts,
                is_edit=True,
                entry=entry,
            )

        entry.date = form.date.data
        entry.description = form.description.data
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
        flash(f"伝票 #{entry.entry_number} を更新しました。", "success")
        return redirect(url_for("journal.index"))

    if request.method == "GET":
        form.date.data = entry.date
        form.description.data = entry.description
        form.fiscal_period.data = str(entry.fiscal_period) if entry.fiscal_period is not None else ""

    # Lv2: 公開行 + 事業主集約行
    if allowed_ids is not None and proprietor_id:
        proprietor_debit = 0
        proprietor_credit = 0
        existing_lines = []
        for line in entry.lines:
            if line.account_id in allowed_ids:
                existing_lines.append({
                    "account_id": line.account_id,
                    "debit_amount": int(line.debit_amount),
                    "credit_amount": int(line.credit_amount),
                    "description": line.description or "",
                })
            else:
                proprietor_debit += int(line.debit_amount)
                proprietor_credit += int(line.credit_amount)
        if proprietor_debit > 0 or proprietor_credit > 0:
            existing_lines.append({
                "account_id": proprietor_id,
                "debit_amount": proprietor_debit,
                "credit_amount": proprietor_credit,
                "description": "",
                "is_proprietor": True,
            })
    else:
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
    user_id = get_effective_user_id()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id
    ).first_or_404()

    allowed_ids = get_allowed_account_ids()
    proprietor_id = get_proprietor_account_id(user_id) if allowed_ids is not None else None

    lines = []
    if allowed_ids is not None and proprietor_id:
        # Lv2: 非公開行を事業主バランス行に集約
        proprietor_debit = 0
        proprietor_credit = 0
        for line in entry.lines:
            if line.account_id in allowed_ids:
                lines.append({
                    "account_id": line.account_id,
                    "debit_amount": int(line.debit_amount),
                    "credit_amount": int(line.credit_amount),
                    "description": line.description or "",
                })
            else:
                proprietor_debit += int(line.debit_amount)
                proprietor_credit += int(line.credit_amount)
        if proprietor_debit > 0 or proprietor_credit > 0:
            lines.append({
                "account_id": proprietor_id,
                "debit_amount": proprietor_debit,
                "credit_amount": proprietor_credit,
                "description": "",
                "is_proprietor": True,
            })
    else:
        for line in entry.lines:
            lines.append({
                "account_id": line.account_id,
                "debit_amount": int(line.debit_amount),
                "credit_amount": int(line.credit_amount),
                "description": line.description or "",
            })

    # ロック判定
    is_locked = False
    if not is_acting_as_auditor():
        is_locked = is_entry_locked_for_owner(user_id, entry)

    return jsonify({
        "id": entry.id,
        "date": entry.date.isoformat(),
        "description": entry.description,
        "entry_number": entry.entry_number,
        "fiscal_period": entry.fiscal_period,
        "is_locked": is_locked,
        "lines": lines,
    })


@bp.route("/<int:entry_id>/edit-api", methods=["POST"])
@login_required
def edit_api(entry_id):
    """仕訳をJSON APIで更新する（モーダル編集用）"""
    user_id = get_effective_user_id()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id
    ).first_or_404()

    allowed_ids = get_allowed_account_ids()

    # 伝票ロック: 本人側
    if not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, entry):
        return jsonify({"error": "提出済みの税務科目を含む伝票のため変更できません。"}), 400

    # 確定済み期間チェック
    err = check_entry_modifiable(user_id, entry)
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

    # Lv2: 事業主行（is_proprietor）を除外して公開科目の行のみ受け入れ
    proprietor_id = get_proprietor_account_id(user_id) if allowed_ids is not None else None

    parsed = []
    for line in lines_data:
        aid = int(line["account_id"])
        # Lv2: 事業主行はスキップ（非公開行はDBに保持）
        if allowed_ids is not None and proprietor_id and aid == proprietor_id:
            continue
        parsed.append({
            "account_id": aid,
            "debit_amount": int(line.get("debit_amount", 0) or 0),
            "credit_amount": int(line.get("credit_amount", 0) or 0),
            "description": line.get("description", ""),
        })

    if allowed_ids is not None:
        # Lv2: 非公開行を保持したまま、公開行だけ差し替え
        # 非公開行の借方/貸方合計を算出
        non_public_debit = 0
        non_public_credit = 0
        non_public_lines = []
        for line in entry.lines:
            if line.account_id not in allowed_ids:
                non_public_debit += int(line.debit_amount)
                non_public_credit += int(line.credit_amount)
                non_public_lines.append(line)

        # 公開行の貸借チェック（非公開行を含めた全体で確認）
        public_debit = sum(l["debit_amount"] for l in parsed)
        public_credit = sum(l["credit_amount"] for l in parsed)
        total_debit = public_debit + non_public_debit
        total_credit = public_credit + non_public_credit
        if total_debit != total_credit:
            return jsonify({
                "error": f"貸借が一致しません（借方: {total_debit:,}, 貸方: {total_credit:,}）"
            }), 400

        # 計上期間の決定
        raw_period = data.get("fiscal_period")
        fiscal_period = int(raw_period) if raw_period not in (None, "") else None
        new_date = adjust_date_for_fiscal_period(date.fromisoformat(entry_date), fiscal_period)

        # 変更先の期間が確定済みでないかチェック
        new_period = fiscal_period if fiscal_period is not None else new_date.month
        err = check_period_open_for_new(user_id, new_date.year, new_period)
        if err:
            return jsonify({"error": err}), 400

        entry.date = new_date
        entry.description = description
        entry.fiscal_period = fiscal_period

        # 公開科目の既存行だけ削除
        for line in entry.lines:
            if line.account_id in allowed_ids:
                db.session.delete(line)
        db.session.flush()

        # 公開科目の新しい行を追加
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

    # 通常ユーザー / Lv3: 全行差し替え
    total_debit = sum(l["debit_amount"] for l in parsed)
    total_credit = sum(l["credit_amount"] for l in parsed)
    if total_debit != total_credit:
        return jsonify({
            "error": f"貸借が一致しません（借方: {total_debit:,}, 貸方: {total_credit:,}）"
        }), 400

    # 計上期間の決定
    raw_period = data.get("fiscal_period")
    fiscal_period = int(raw_period) if raw_period not in (None, "") else None
    new_date = adjust_date_for_fiscal_period(date.fromisoformat(entry_date), fiscal_period)

    # 変更先の期間が確定済みでないかチェック
    new_period = fiscal_period if fiscal_period is not None else new_date.month
    err = check_period_open_for_new(user_id, new_date.year, new_period)
    if err:
        return jsonify({"error": err}), 400

    entry.date = new_date
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
    user_id = get_effective_user_id()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id
    ).first_or_404()

    allowed_ids = get_allowed_account_ids()

    # 伝票ロックチェック
    if not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, entry):
        flash("提出済みの税務科目を含む伝票のため削除できません。", "danger")
        return redirect(url_for("journal.index"))
    if is_acting_as_auditor() and allowed_ids is not None and is_entry_locked_for_auditor(entry, allowed_ids):
        flash("事業主勘定を含む伝票のため削除できません。", "danger")
        return redirect(url_for("journal.index"))

    # 確定済み期間チェック
    err = check_entry_modifiable(user_id, entry)
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
        JournalEntry.user_id == get_effective_user_id(),
    ).all()

    user_id = get_effective_user_id()
    allowed_ids = get_allowed_account_ids()

    # 確定済み期間 + 伝票ロックチェック
    locked = []
    deletable = []
    for entry in entries:
        err = check_entry_modifiable(user_id, entry)
        if err:
            locked.append(entry)
        elif not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, entry):
            locked.append(entry)
        elif is_acting_as_auditor() and allowed_ids is not None and is_entry_locked_for_auditor(entry, allowed_ids):
            locked.append(entry)
        else:
            deletable.append(entry)

    if locked:
        flash(f"{len(locked)}件の仕訳は削除できませんでした。", "warning")

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
            JournalEntry.user_id == get_effective_user_id(),
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
        user_id=get_effective_user_id(), batch_id=batch_id
    ).all()

    if not entries:
        flash("該当するバッチが見つかりません。", "warning")
        return redirect(url_for("journal.batches"))

    # 確定済み期間チェック
    locked = []
    deletable = []
    for entry in entries:
        err = check_entry_modifiable(get_effective_user_id(), entry)
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
