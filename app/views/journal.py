import json
from datetime import date

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, make_response
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.forms.journal import JournalForm
from app.services.accounting import create_journal_entry, get_next_entry_number
from app.services.fiscal import check_entry_modifiable, check_period_open_for_new, get_effective_period, adjust_date_for_fiscal_period, get_closed_periods_map, get_restricted_before_year, is_period_locked
from app.services.audit import (
    get_effective_user_id, get_allowed_account_codes, get_submitted_account_codes,
    is_entry_locked_for_owner, is_entry_locked_for_auditor,
    is_acting_as_auditor, get_permission_level,
    get_proprietor_account_code, mask_account_name,
)
from app.views.helpers import get_grouped_accounts, is_safe_internal_path, safe_user_error

bp = Blueprint("journal", __name__, url_prefix="/journal")




@bp.route("/")
@login_required
def index():
    page = request.args.get("page", 1, type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    search = request.args.get("search", "")

    allowed_codes = get_allowed_account_codes()

    query = (
        JournalEntry.query
        .filter_by(user_id=get_effective_user_id())
        .options(joinedload(JournalEntry.vouchers))
        .order_by(JournalEntry.date.desc(), JournalEntry.entry_number.desc())
    )

    # Lv2: 公開科目を1つも含まない伝票を除外
    if allowed_codes is not None:
        query = query.filter(
            JournalEntry.id.in_(
                db.session.query(JournalEntryLine.journal_entry_id)
                .filter(JournalEntryLine.account_code.in_(allowed_codes))
            )
        )

    if date_from:
        query = query.filter(JournalEntry.date >= date_from)
    if date_to:
        query = query.filter(JournalEntry.date <= date_to)
    if search:
        query = query.filter(JournalEntry.description.ilike(f"%{search}%"))

    entries = query.paginate(page=page, per_page=20, error_out=False)

    # 各 entry の編集可否フラグを付与
    user_id = get_effective_user_id()
    for entry in entries.items:
        entry._modifiable = (
            check_entry_modifiable(user_id, entry) is None
            and not is_entry_locked_for_owner(user_id, entry)
        )

    return render_template(
        "journal/index.html",
        entries=entries,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )


# E3-F PR-B2: new() / edit() は GET 専用。フォーム送信は JS が
# entries_builder.buildJournalEntry で暗号化 → POST /api/v1/journals/batch (新規)
# / PUT /api/v1/journals/<id> (更新) に直接送る (E2EE 経路)。
# accounting.create_journal_entry は本 view からは呼ばれなくなったが、ai_journal /
# auto_import / tests 由来の呼出が残るため関数自体は dual-storage 完了 (PR-D) まで
# 保持する。
# Lv2 監査者の科目フィルタ (proprietor 集約 / 部分置換) は edit_api / create_api
# (JSON API、平文経路) で従来通り。GET の existing_lines にも proprietor 集約は
# 残すが、JS submit 側で is_proprietor 行を除外して送る。
@bp.route("/new", methods=["GET"])
@login_required
def new():
    form = JournalForm()
    user_id = get_effective_user_id()
    allowed_codes = get_allowed_account_codes()
    grouped_accounts = get_grouped_accounts(user_id, allowed_codes)
    closed_periods = get_closed_periods_map(user_id)
    restricted_before = get_restricted_before_year(user_id)

    if not form.date.data:
        form.date.data = date.today()

    return render_template(
        "journal/form.html",
        form=form,
        grouped_accounts=grouped_accounts,
        is_edit=False,
        closed_periods=closed_periods,
        restricted_before_year=restricted_before,
    )


@bp.route("/<int:entry_id>/edit", methods=["GET"])
@login_required
def edit(entry_id):
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=get_effective_user_id()
    ).first_or_404()

    user_id = get_effective_user_id()
    allowed_codes = get_allowed_account_codes()

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
    grouped_accounts = get_grouped_accounts(get_effective_user_id(), allowed_codes)
    proprietor_code = get_proprietor_account_code(user_id) if allowed_codes is not None else None
    closed_periods = get_closed_periods_map(user_id)
    restricted_before = get_restricted_before_year(user_id)

    form.date.data = entry.date
    form.description.data = entry.description
    form.fiscal_period.data = str(entry.fiscal_period) if entry.fiscal_period is not None else ""

    # Lv2: 公開行 + 事業主集約行
    if allowed_codes is not None and proprietor_code:
        proprietor_debit = 0
        proprietor_credit = 0
        existing_lines = []
        for line in entry.lines:
            if line.account_code in allowed_codes:
                existing_lines.append({
                    "account_code": line.account_code,
                    "debit_amount": int(line.debit_amount),
                    "credit_amount": int(line.credit_amount),
                    "description": line.description or "",
                })
            else:
                proprietor_debit += int(line.debit_amount)
                proprietor_credit += int(line.credit_amount)
        if proprietor_debit > 0 or proprietor_credit > 0:
            existing_lines.append({
                "account_code": proprietor_code,
                "debit_amount": proprietor_debit,
                "credit_amount": proprietor_credit,
                "description": "",
                "is_proprietor": True,
            })
    else:
        existing_lines = [
            {
                "account_code": line.account_code,
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
        existing_lines=existing_lines,
        closed_periods=closed_periods,
        restricted_before_year=restricted_before,
    )


@bp.route("/<int:entry_id>/json")
@login_required
def get_json(entry_id):
    """仕訳データをJSON形式で返す（モーダル編集用）"""
    user_id = get_effective_user_id()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id
    ).first_or_404()

    allowed_codes = get_allowed_account_codes()
    proprietor_code = get_proprietor_account_code(user_id) if allowed_codes is not None else None

    lines = []
    if allowed_codes is not None and proprietor_code:
        # Lv2: 非公開行を事業主バランス行に集約
        proprietor_debit = 0
        proprietor_credit = 0
        for line in entry.lines:
            if line.account_code in allowed_codes:
                lines.append({
                    "account_code": line.account_code,
                    "debit_amount": int(line.debit_amount),
                    "credit_amount": int(line.credit_amount),
                    "description": line.description or "",
                })
            else:
                proprietor_debit += int(line.debit_amount)
                proprietor_credit += int(line.credit_amount)
        if proprietor_debit > 0 or proprietor_credit > 0:
            lines.append({
                "account_code": proprietor_code,
                "debit_amount": proprietor_debit,
                "credit_amount": proprietor_credit,
                "description": "",
                "is_proprietor": True,
            })
    else:
        for line in entry.lines:
            lines.append({
                "account_code": line.account_code,
                "debit_amount": int(line.debit_amount),
                "credit_amount": int(line.credit_amount),
                "description": line.description or "",
            })

    # ロック判定（確定済み期間・損益振替・提出ロック）
    is_readonly = check_entry_modifiable(user_id, entry) is not None
    if not is_readonly and not is_acting_as_auditor():
        is_readonly = is_entry_locked_for_owner(user_id, entry)

    return jsonify({
        "id": entry.id,
        "date": entry.date.isoformat(),
        "description": entry.description,
        "entry_number": entry.entry_number,
        "fiscal_period": entry.fiscal_period,
        "is_readonly": is_readonly,
        "source": entry.source,
        "has_voucher": len(entry.active_vouchers) > 0,
        "lines": lines,
        "vouchers": [
            {"id": v.id, "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None}
            for v in entry.active_vouchers
        ],
    })


@bp.route("/<int:entry_id>/edit-api", methods=["POST"])
@login_required
def edit_api(entry_id):
    """仕訳をJSON APIで更新する（モーダル編集用）"""
    user_id = get_effective_user_id()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id
    ).first_or_404()

    allowed_codes = get_allowed_account_codes()

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
    proprietor_code = get_proprietor_account_code(user_id) if allowed_codes is not None else None

    parsed = []
    for line in lines_data:
        acode = line["account_code"]
        # Lv2: 事業主行はスキップ（非公開行はDBに保持）
        if allowed_codes is not None and proprietor_code and acode == proprietor_code:
            continue
        parsed.append({
            "account_code": acode,
            "debit_amount": int(line.get("debit_amount", 0) or 0),
            "credit_amount": int(line.get("credit_amount", 0) or 0),
            "description": line.get("description", ""),
        })

    if allowed_codes is not None:
        # Lv2: 非公開行を保持したまま、公開行だけ差し替え
        # 非公開行の借方/貸方合計を算出
        non_public_debit = 0
        non_public_credit = 0
        non_public_lines = []
        for line in entry.lines:
            if line.account_code not in allowed_codes:
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
        if fiscal_period == 16:
            return jsonify({"error": "損益振替期間には手動で仕訳を追加できません。"}), 400
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
            if line.account_code in allowed_codes:
                db.session.delete(line)
        db.session.flush()

        # 公開科目の新しい行を追加
        for line_data in parsed:
            db.session.add(JournalEntryLine(
                journal_entry_id=entry.id,
                account_user_id=user_id,
                account_code=line_data["account_code"],
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
    if fiscal_period == 16:
        return jsonify({"error": "損益振替期間には手動で仕訳を追加できません。"}), 400
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
            account_user_id=user_id,
            account_code=line_data["account_code"],
            debit_amount=line_data["debit_amount"],
            credit_amount=line_data["credit_amount"],
            description=line_data.get("description", ""),
        ))

    db.session.commit()
    return jsonify({"ok": True, "entry_number": entry.entry_number})


@bp.route("/create-api", methods=["POST"])
@login_required
def create_api():
    """仕訳をJSON APIで新規作成する（モーダル複写用）"""
    user_id = get_effective_user_id()
    data = request.get_json()
    if not data:
        return jsonify({"error": "リクエストが不正です。"}), 400

    entry_date_str = data.get("date")
    description = data.get("description", "").strip()
    lines_data = data.get("lines", [])

    if not entry_date_str or not description:
        return jsonify({"error": "日付と摘要は必須です。"}), 400
    if not lines_data:
        return jsonify({"error": "仕訳明細を1行以上入力してください。"}), 400

    parsed = []
    for line in lines_data:
        acode = line.get("account_code")
        if not acode:
            continue
        parsed.append({
            "account_code": acode,
            "debit_amount": int(line.get("debit_amount", 0) or 0),
            "credit_amount": int(line.get("credit_amount", 0) or 0),
            "description": line.get("description", ""),
        })

    if not parsed:
        return jsonify({"error": "仕訳明細を1行以上入力してください。"}), 400

    total_debit = sum(l["debit_amount"] for l in parsed)
    total_credit = sum(l["credit_amount"] for l in parsed)
    if total_debit != total_credit:
        return jsonify({
            "error": f"貸借が一致しません（借方: {total_debit:,}, 貸方: {total_credit:,}）"
        }), 400

    raw_period = data.get("fiscal_period")
    fiscal_period = int(raw_period) if raw_period not in (None, "") else None
    if fiscal_period == 16:
        return jsonify({"error": "損益振替期間には手動で仕訳を追加できません。"}), 400

    try:
        entry_date = date.fromisoformat(entry_date_str)
    except ValueError:
        return jsonify({"error": "日付の形式が不正です。"}), 400

    entry_date = adjust_date_for_fiscal_period(entry_date, fiscal_period)
    period = fiscal_period if fiscal_period is not None else entry_date.month
    err = check_period_open_for_new(user_id, entry_date.year, period)
    if err:
        return jsonify({"error": err}), 400

    # 提出済みロック科目チェック
    locked_codes = get_submitted_account_codes(user_id)
    if locked_codes:
        used_codes = {l["account_code"] for l in parsed}
        if used_codes & locked_codes:
            return jsonify({"error": "提出済みの税務科目を含むため登録できません。"}), 400

    try:
        entry = create_journal_entry(
            user_id=user_id,
            date=entry_date,
            description=description,
            lines_data=parsed,
            source="journal",
            fiscal_period=fiscal_period,
        )
        db.session.commit()
        return jsonify({"ok": True, "entry_number": entry.entry_number})
    except ValueError as e:
        from flask import current_app
        current_app.logger.exception("create_journal_entry failed (journal)")
        return jsonify({"error": safe_user_error(e)}), 400


def log_voucher_orphan(entry, user_id):
    """仕訳削除前に紐づく証憑の孤立化ログを記録する。"""
    # 論理削除済も含めて orphan ログを残す (削除済 Voucher の AuditLog も
    # 「仕訳が消えた」事実を追記すべき。電帳法の連環的な証跡保全)
    vouchers = Voucher.query.filter_by(journal_entry_id=entry.id).all()
    for v in vouchers:
        db.session.add(VoucherAuditLog(
            voucher_id=v.id,
            user_id=user_id,
            action="orphaned",
            detail=json.dumps({
                "journal_entry_id": entry.id,
                "entry_number": entry.entry_number,
                "description": entry.description,
            }, ensure_ascii=False),
        ))


@bp.route("/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete(entry_id):
    is_htmx = bool(request.headers.get("HX-Request"))
    is_ajax = is_htmx or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    user_id = get_effective_user_id()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id
    ).first_or_404()

    allowed_codes = get_allowed_account_codes()

    # 伝票ロックチェック
    err_msg = None
    if not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, entry):
        err_msg = "提出済みの税務科目を含む伝票のため削除できません。"
    elif is_acting_as_auditor() and allowed_codes is not None and is_entry_locked_for_auditor(entry, allowed_codes):
        err_msg = "事業主勘定を含む伝票のため削除できません。"
    else:
        err_msg = check_entry_modifiable(user_id, entry)

    if err_msg:
        if is_htmx:
            resp = make_response("", 422)
            resp.headers["HX-Reswap"] = "none"
            resp.headers["HX-Trigger"] = json.dumps(
                {"showToast": {"message": err_msg, "type": "danger"}}
            )
            return resp
        if is_ajax:
            return jsonify({"ok": False, "message": err_msg}), 400
        flash(err_msg, "danger")
        return redirect(url_for("journal.index"))

    num = entry.entry_number
    log_voucher_orphan(entry, user_id)
    db.session.delete(entry)
    db.session.commit()

    msg = f"伝票 #{num} を削除しました。"
    if is_htmx:
        resp = make_response("", 200)
        resp.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": msg, "type": "success"}}
        )
        return resp
    if is_ajax:
        return jsonify({"ok": True, "message": msg})
    flash(msg, "success")
    return redirect(url_for("journal.index"))


@bp.route("/bulk-delete", methods=["POST"])
@login_required
def bulk_delete():
    """仕訳の一括削除"""
    entry_ids = request.form.getlist("entry_ids", type=int)
    raw_redirect = request.form.get("redirect_url", "")
    fallback_url = url_for("journal.index")
    redirect_url = raw_redirect if is_safe_internal_path(raw_redirect) else fallback_url

    if not entry_ids:
        flash("削除する仕訳が選択されていません。", "warning")
        return redirect(redirect_url)

    entries = JournalEntry.query.filter(
        JournalEntry.id.in_(entry_ids),
        JournalEntry.user_id == get_effective_user_id(),
    ).all()

    user_id = get_effective_user_id()
    allowed_codes = get_allowed_account_codes()

    # 確定済み期間 + 伝票ロックチェック
    locked = []
    deletable = []
    for entry in entries:
        err = check_entry_modifiable(user_id, entry)
        if err:
            locked.append(entry)
        elif not is_acting_as_auditor() and is_entry_locked_for_owner(user_id, entry):
            locked.append(entry)
        elif is_acting_as_auditor() and allowed_codes is not None and is_entry_locked_for_auditor(entry, allowed_codes):
            locked.append(entry)
        else:
            deletable.append(entry)

    if locked:
        flash(f"{len(locked)}件の仕訳は削除できませんでした。", "warning")

    count = len(deletable)
    for entry in deletable:
        log_voucher_orphan(entry, user_id)
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
    user_id = get_effective_user_id()
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
            JournalEntry.user_id == user_id,
            JournalEntry.batch_id.isnot(None),
        )
        .group_by(JournalEntry.batch_id, JournalEntry.source)
        .order_by(func.min(JournalEntry.created_at).desc())
        .all()
    )

    enriched = []
    for b in batch_list:
        deletable = True
        delete_reason = ""
        if b.source == "closing":
            deletable = False
            delete_reason = "損益振替（自動生成）は削除できません"
        else:
            # バッチの日付範囲に含まれる全月をチェック
            d = b.date_from
            while d <= b.date_to:
                if is_period_locked(user_id, d.year, d.month):
                    deletable = False
                    delete_reason = "確定済み期間の仕訳が含まれています"
                    break
                if d.month == 12:
                    d = date(d.year + 1, 1, 1)
                else:
                    d = date(d.year, d.month + 1, 1)
        enriched.append({
            "batch_id": b.batch_id,
            "source": b.source,
            "count": b.count,
            "date_from": b.date_from,
            "date_to": b.date_to,
            "imported_at": b.imported_at,
            "deletable": deletable,
            "delete_reason": delete_reason,
        })

    return render_template(
        "journal/batches.html",
        batches=enriched,
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
    user_id = get_effective_user_id()
    for entry in deletable:
        log_voucher_orphan(entry, user_id)
        db.session.delete(entry)
    db.session.commit()
    flash(f"{count}件の仕訳を削除しました。", "success")
    return redirect(url_for("journal.batches"))


@bp.route("/api/suggest-categories", methods=["POST"])
@login_required
def suggest_categories():
    """摘要から過去の仕訳の科目を推定して返す

    POST: {"descriptions": ["摘要1", "摘要2", ...], "payment_account_code": "1010"}
    Response: {"摘要1": {"account_code": "5010", "account_name": "食費"}, ...}
    """
    data = request.get_json()
    if not data:
        return jsonify({}), 400

    descriptions = data.get("descriptions", [])
    payment_account_code = data.get("payment_account_code")
    if not descriptions:
        return jsonify({})

    user_id = get_effective_user_id()
    unique_descs = list(set(d for d in descriptions if d))
    if not unique_descs:
        return jsonify({})

    # 摘要ごとに最新の仕訳から相手科目を取得
    result = {}
    for desc in unique_descs:
        entry = (
            JournalEntry.query
            .filter(
                JournalEntry.user_id == user_id,
                JournalEntry.description == desc,
            )
            .order_by(JournalEntry.date.desc(), JournalEntry.id.desc())
            .first()
        )
        if not entry:
            continue

        # 支払口座以外の科目を取得（= 相手科目）
        for line in entry.lines:
            if payment_account_code and line.account_code == payment_account_code:
                continue
            account = Account.query.filter_by(user_id=user_id, code=line.account_code).first()
            if account and account.is_active:
                result[desc] = {
                    "account_code": account.code,
                    "account_name": account.name,
                }
                break

    return jsonify(result)


# /journal/api/ai-suggest-categories は廃止。
# クライアントが直接 /api/v1/suggest-categories/prompt-context + ai-config +
# クライアント側 LLM 呼出 (suggest_categories_orchestrator.js) で科目推定を
# 行う。サーバには raw description も API キーも届かない。
