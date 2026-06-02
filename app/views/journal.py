import json
from datetime import date

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, make_response
from flask_login import login_required, current_user

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.forms.journal import JournalForm
from app.services.accounting import create_journal_entry
from app.services.fiscal import check_entry_modifiable, get_effective_period, get_closed_periods_map, get_restricted_before_year
from app.services.audit import (
    get_effective_user_id, get_allowed_account_codes,
    is_entry_locked_for_auditor,
    is_acting_as_auditor, get_permission_level,
    get_proprietor_account_code, mask_account_name,
)
from app.views.helpers import get_grouped_accounts, is_safe_internal_path, safe_user_error

bp = Blueprint("journal", __name__, url_prefix="/journal")




def _journal_accounts_meta(user_id):
    """仕訳帳一覧 (journal/index.html) のクライアント描画が科目名解決に使う
    code → {name} メタ。account テーブルは非暗号化メタデータなのでサーバ側で
    構築してよい。仕訳は無効化済み科目も参照しうるため is_active で絞らず全科目を
    含める。監査 Lv2 では allowed_codes でフィルタ + 非公開科目名はマスクする
    (代理閲覧時はクライアント側で復号できず空表示になる)。
    """
    allowed_codes = get_allowed_account_codes()
    accounts = (
        Account.query.filter_by(user_id=user_id)
        .order_by(Account.code)
        .all()
    )
    if allowed_codes is not None:
        accounts = [a for a in accounts if a.code in allowed_codes]
    return {
        a.code: {"name": mask_account_name(a.name, a.code, allowed_codes)}
        for a in accounts
    }


@bp.route("/")
@login_required
def index():
    """仕訳帳一覧 (E3-F PR-D-4-3 でクライアント描画に移行)。

    クライアントが /api/v1/journals を fiscal_year で取得・MK 復号し、編集可否
    (modifiable) を closed_periods から算出してテーブルを描画する。
    サーバ側で平文 (date / description / line.account 名) は一切読まない。旧
    date_from/date_to 範囲 filter は fiscal_year セレクタに、description.ilike 検索
    はクライアント側の摘要絞り込みに置換 (date / description は DROP 対象の平文)。
    """
    year = request.args.get("year", date.today().year, type=int)
    user_id = get_effective_user_id()
    return render_template(
        "journal/index.html",
        year=year,
        effective_user_id=user_id,
        accounts_meta=_journal_accounts_meta(user_id),
        closed_periods=get_closed_periods_map(user_id),
    )


# E3-F PR-B2: new() / edit() は GET 専用。フォーム送信は JS が
# entries_builder.buildJournalEntry で暗号化 → POST /api/v1/journals/batch (新規)
# / PUT /api/v1/journals/<id> (更新) に直接送る (E2EE 経路)。
# accounting.create_journal_entry は本 view からは呼ばれなくなったが、ai_journal /
# auto_import / tests 由来の呼出が残るため関数自体は dual-storage 完了 (PR-D) まで
# 保持する。
# get_json は編集モーダルの初期表示データを返す (Lv2 では非公開行を proprietor
# 行に集約)。保存は暗号化済み API 側で行うため、Lv2 監査代理での暗号化 write は
# AAD 不一致で復号不能になり PUT/batch のガードでブロックされる (E2EE 仕様)。
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

    # E3-F PR-D-6-3b-3: 平文 date / description の prefill 読取を撤去
    # (これらの列は D-6-5 で DROP)。クライアント (edit_form_prefill.js) が
    # encrypted_blob を MK で復号して date / description フィールドを埋める。
    # E3-F PR-D-6-3: fiscal_period prefill は保持列 fiscal_month から行う
    # (両者は書込時に同期されており等価)。
    form.fiscal_period.data = str(entry.fiscal_month) if entry.fiscal_month is not None else ""

    # Lv2: 公開行 + 事業主集約行
    # E3-F PR-D-6-5-pre2: 行摘要 (description) は平文列を読まず空で返し、編集
    # フォームのクライアント hydration (edit_form_prefill.js) が line の
    # encrypted_blob を復号して line id で対応行へ埋める。id を付与してマッチング
    # 可能にする (集約行は合成なので id=None)。
    if allowed_codes is not None and proprietor_code:
        proprietor_debit = 0
        proprietor_credit = 0
        existing_lines = []
        for line in entry.lines:
            if line.account_code in allowed_codes:
                existing_lines.append({
                    "id": line.id,
                    "account_code": line.account_code,
                    "debit_amount": int(line.debit_amount),
                    "credit_amount": int(line.credit_amount),
                    "description": "",
                })
            else:
                proprietor_debit += int(line.debit_amount)
                proprietor_credit += int(line.credit_amount)
        if proprietor_debit > 0 or proprietor_credit > 0:
            existing_lines.append({
                "id": None,
                "account_code": proprietor_code,
                "debit_amount": proprietor_debit,
                "credit_amount": proprietor_credit,
                "description": "",
                "is_proprietor": True,
            })
    else:
        existing_lines = [
            {
                "id": line.id,
                "account_code": line.account_code,
                "debit_amount": int(line.debit_amount),
                "credit_amount": int(line.credit_amount),
                "description": "",
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


def _b64_or_none(b):
    """LargeBinary カラム → base64 文字列 (None なら None)。"""
    from base64 import b64encode
    return b64encode(b).decode("ascii") if b else None


@bp.route("/<int:entry_id>/json")
@login_required
def get_json(entry_id):
    """仕訳データをJSON形式で返す（モーダル編集用）

    E3-F PR-D-6-3b-3: entry レベルの平文 date / description / fiscal_period /
    source は返さず encrypted_blob / blob_iv を返す。クライアント (元帳モーダル)
    が MK で復号して取り出す。lines / is_readonly / vouchers はサーバ側ロジック
    を要するため引き続きサーバが算出する。
    """
    user_id = get_effective_user_id()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id
    ).first_or_404()

    allowed_codes = get_allowed_account_codes()
    proprietor_code = get_proprietor_account_code(user_id) if allowed_codes is not None else None

    # E3-F PR-D-6-5-pre2: 行摘要 (description) は平文列を読まず空で返し、line の
    # encrypted_blob / blob_iv / id を返す。元帳モーダル (reports/ledger.html
    # openEditModal) が line blob を自分の MK で復号して line id で対応行へ埋める。
    # 集約行は合成なので id / blob は None。
    lines = []
    if allowed_codes is not None and proprietor_code:
        # Lv2: 非公開行を事業主バランス行に集約
        proprietor_debit = 0
        proprietor_credit = 0
        for line in entry.lines:
            if line.account_code in allowed_codes:
                lines.append({
                    "id": line.id,
                    "account_code": line.account_code,
                    "debit_amount": int(line.debit_amount),
                    "credit_amount": int(line.credit_amount),
                    "description": "",
                    "encrypted_blob": _b64_or_none(line.encrypted_blob),
                    "blob_iv": _b64_or_none(line.blob_iv),
                })
            else:
                proprietor_debit += int(line.debit_amount)
                proprietor_credit += int(line.credit_amount)
        if proprietor_debit > 0 or proprietor_credit > 0:
            lines.append({
                "id": None,
                "account_code": proprietor_code,
                "debit_amount": proprietor_debit,
                "credit_amount": proprietor_credit,
                "description": "",
                "is_proprietor": True,
                "encrypted_blob": None,
                "blob_iv": None,
            })
    else:
        for line in entry.lines:
            lines.append({
                "id": line.id,
                "account_code": line.account_code,
                "debit_amount": int(line.debit_amount),
                "credit_amount": int(line.credit_amount),
                "description": "",
                "encrypted_blob": _b64_or_none(line.encrypted_blob),
                "blob_iv": _b64_or_none(line.blob_iv),
            })

    # ロック判定（確定済み期間・損益振替）
    is_readonly = check_entry_modifiable(user_id, entry) is not None

    # E3-F PR-D-6-3b-3: 平文 date / description / fiscal_period / source の返却を
    # 撤去 (これらの列は D-6-5 で DROP)。元帳モーダル (reports/ledger.html
    # openEditModal) は encrypted_blob を自分の MK で復号して date / description
    # / fiscal_period を取り出す (decryptEntryMeta)。closing 仕訳 (暗号化不能で
    # encrypted_blob 空) は is_closing / fiscal_year から合成する。
    # is_readonly / lines (Lv2 集約済) / vouchers はサーバ側ロジックを要するため
    # 引き続きサーバが算出して返す。
    return jsonify({
        "id": entry.id,
        "entry_number": entry.entry_number,
        "is_readonly": is_readonly,
        "is_closing": entry.is_closing,
        "fiscal_year": entry.fiscal_year,
        "fiscal_month": entry.fiscal_month,
        "encrypted_blob": _b64_or_none(entry.encrypted_blob),
        "blob_iv": _b64_or_none(entry.blob_iv),
        "has_voucher": len(entry.active_vouchers) > 0,
        "lines": lines,
        "vouchers": [
            {"id": v.id, "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None}
            for v in entry.active_vouchers
        ],
    })


def log_voucher_orphan(entry, user_id):
    """仕訳削除前に紐づく証憑の孤立化ログを記録する。"""
    # 論理削除済も含めて orphan ログを残す (削除済 Voucher の AuditLog も
    # 「仕訳が消えた」事実を追記すべき。電帳法の連環的な証跡保全)
    vouchers = Voucher.query.filter_by(journal_entry_id=entry.id).all()
    for v in vouchers:
        # E4 PR-D: 平文 detail は記録しない。"orphaned" action + voucher_id +
        # created_at で「紐付け仕訳が削除された事実」(電帳法 訂正削除の事実) は
        # 担保される。削除された仕訳の entry_number 自体も消えるため、detail に
        # 控えても整合する参照先は残らない (encrypted_detail_blob は将来のクラ
        # イアント供給暗号化ノート用、valog AAD)。
        db.session.add(VoucherAuditLog(
            voucher_id=v.id,
            user_id=user_id,
            action="orphaned",
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
    if is_acting_as_auditor() and allowed_codes is not None and is_entry_locked_for_auditor(entry, allowed_codes):
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
    """インポート履歴 (クライアント描画)。

    E3-F PR-D-6-3b-2: 旧実装はサーバが平文 JournalEntry.date / source を
    集計してテーブルを描画していたが、E2EE 化 (date / source 列は D-6-5 で
    DROP 予定) のためクライアント描画へ移行した。バッチ一覧は
    GET /api/v1/journals/batches から取得し、復号 blob から種別ラベルと
    日付範囲を組み立てる (batches_renderer.mjs)。件数 / 取込日時 / 削除可否は
    保持列由来でサーバ (API) 側が算出する。
    """
    return render_template(
        "journal/batches.html",
        effective_user_id=get_effective_user_id(),
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


# /journal/api/suggest-categories は E3-F PR-D-4 で廃止。
# 旧実装は平文 JournalEntry.description / JournalEntry.date を読んで
# 「同一摘要の最新仕訳の相手科目」を返していたが、E2EE 化に伴い平文読取を
# 撤去した。クライアントが復号済み仕訳から推定する
# (crypto/suggest_categories_classical.js + alpine runSuggestCategoriesClassical)。
# サーバには raw description が届かない。

# /journal/api/ai-suggest-categories は廃止。
# クライアントが直接 /api/v1/suggest-categories/prompt-context + ai-config +
# クライアント側 LLM 呼出 (suggest_categories_orchestrator.js) で科目推定を
# 行う。サーバには raw description も API キーも届かない。
