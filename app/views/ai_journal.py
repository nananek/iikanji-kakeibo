"""AI証憑仕訳ビュー"""

import json
from datetime import date as date_type

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, session,
    jsonify,
)
from flask_login import login_required

from app.extensions import db
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.user import User
from app.services.audit import get_effective_user_id
from app.services.fiscal import get_closed_periods_map, get_restricted_before_year
from app.services.image import serve_draft_image, serve_voucher_image
from app.services.storage import (
    get_storage_backend, make_thumbnail_key,
)
from app.services.storage_quota import record_delete
from app.models.voucher import Voucher
from app.views.helpers import get_grouped_accounts, check_deadline

bp = Blueprint("ai_journal", __name__, url_prefix="/ai-journal")


@bp.route("/", methods=["GET"])
@login_required
def upload():
    """証憑画像アップロード画面"""
    user_id = get_effective_user_id()
    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    draft_count = AIDraft.query.filter_by(
        user_id=user_id, status="analyzed"
    ).count()
    # 前回の一時ドラフトをクリーンアップ
    temp_drafts = AIDraft.query.filter_by(user_id=user_id, status="temp").all()
    keys_to_delete = [d.image_key for d in temp_drafts]
    # Phase 5 #70: temp ドラフトは Voucher 化されていない = 計上分が宙ぶらりん
    # のため、削除と同時に record_delete で StorageUsage を減算する。
    # file_size NULL のレガシードラフトは減算対象外。
    sizes_to_release = sum(
        d.file_size for d in temp_drafts if d.file_size
    )
    for d in temp_drafts:
        db.session.delete(d)
    db.session.commit()
    storage = get_storage_backend()
    for key in keys_to_delete:
        storage.delete(key)
        storage.delete(make_thumbnail_key(key))
    if sizes_to_release > 0:
        owner = db.session.get(User, user_id)
        if owner is not None:
            record_delete(owner, sizes_to_release)
    session.pop("ai_journal_draft_id", None)
    # E2EE 形式の AI 設定が登録済みかをテンプレートに渡す。
    # クライアント側 LLM 呼出 (orchestrator) は is_e2ee=True が必須。
    config_is_e2ee = bool(config and config.is_e2ee)
    return render_template(
        "ai_journal/upload.html",
        has_config=bool(config),
        config_is_e2ee=config_is_e2ee,
        draft_count=draft_count,
        effective_user_id=user_id,
    )


# --- 一時保存 ---

@bp.route("/drafts/save", methods=["POST"])
@login_required
def drafts_save():
    """AJAX: AI解析結果を一時保存する（temp → analyzed に昇格）"""
    draft_id = session.get("ai_journal_draft_id")
    if not draft_id:
        return jsonify({"error": "保存するデータがありません。"}), 400

    draft = AIDraft.query.get(draft_id)
    if not draft or draft.user_id != get_effective_user_id():
        return jsonify({"error": "保存するデータがありません。"}), 400

    draft.status = "analyzed"
    db.session.commit()

    session.pop("ai_journal_draft_id", None)
    return jsonify({"ok": True, "draft_id": draft.id})


@bp.route("/drafts")
@login_required
def drafts():
    """一時保存一覧"""
    user_id = get_effective_user_id()
    items = (
        AIDraft.query
        .filter_by(user_id=user_id)
        .filter_by(status="analyzed")
        .order_by(AIDraft.created_at.desc())
        .all()
    )

    # 各ドラフトのサマリーと案 1 (クライアント側 quick-accept で暗号化送信する元) を抽出
    draft_list = []
    for d in items:
        summary = {}
        suggestion = None
        if d.suggestions_json:
            try:
                suggestions = json.loads(d.suggestions_json)
                if suggestions:
                    s = suggestions[0]
                    suggestion = s
                    summary = {
                        "title": s.get("title", ""),
                        "date": s.get("date", ""),
                        "description": s.get("entry_description", ""),
                        "count": len(suggestions),
                    }
                    # 合計金額を計算
                    lines = s.get("lines", [])
                    total = sum(
                        l.get("debit_amount", 0) for l in lines
                    )
                    summary["amount"] = total
                    # 借方・貸方の科目名
                    summary["debit_accounts"] = [
                        l.get("account_name", l.get("account_code", ""))
                        for l in lines if l.get("debit_amount")
                    ]
                    summary["credit_accounts"] = [
                        l.get("account_name", l.get("account_code", ""))
                        for l in lines if l.get("credit_amount")
                    ]
                    # コンプライアンスチェック結果
                    compliance = s.get("compliance")
                    if isinstance(compliance, dict):
                        summary["compliance_status"] = compliance.get("status")
                        summary["compliance_warnings"] = compliance.get("warnings", [])
                        summary["compliance_details"] = compliance.get("details", [])
                    # 入力期限チェック
                    if summary.get("date"):
                        try:
                            receipt_date = date_type.fromisoformat(summary["date"])
                            summary["deadline_exceeded"] = check_deadline(
                                receipt_date, d.created_at,
                            )
                        except ValueError:
                            pass
            except (json.JSONDecodeError, IndexError):
                pass
        draft_list.append({
            "draft": d,
            "summary": summary,
            "suggestion": suggestion,
        })

    return render_template(
        "ai_journal/drafts.html",
        draft_list=draft_list,
        # E5 (#111): 暗号化下書きサムネのクライアント復号 (AAD 束縛) 用。
        # 下書きは全て effective_user_id (= 表示中ユーザー) の所有。
        effective_user_id=user_id,
    )


@bp.route("/drafts/<int:draft_id>/image")
@login_required
def draft_image(draft_id):
    """ドラフトの画像を返す"""
    draft = AIDraft.query.get_or_404(draft_id)
    if draft.user_id != get_effective_user_id():
        return "", 403
    try:
        # E5 (#111): E2EE 下書き (encrypted_meta_blob) は octet-stream 配信、
        # レガシー平文下書きは image_mime 配信 (serve_draft_image が両対応)。
        return serve_draft_image(draft)
    except FileNotFoundError:
        return "", 404


@bp.route("/voucher/<int:voucher_id>/image")
@login_required
def voucher_image(voucher_id):
    """証憑画像を返す"""
    voucher = Voucher.active().filter_by(id=voucher_id).first_or_404()
    if voucher.user_id != get_effective_user_id():
        return "", 403
    try:
        return serve_voucher_image(voucher)
    except FileNotFoundError:
        return "", 404


@bp.route("/drafts/<int:draft_id>/delete", methods=["POST"])
@login_required
def drafts_delete(draft_id):
    """ドラフト削除"""
    draft = AIDraft.query.get_or_404(draft_id)
    if draft.user_id != get_effective_user_id():
        flash("権限がありません。", "danger")
        return redirect(url_for("ai_journal.drafts"))

    image_key = draft.image_key
    # Phase 5 #70: AIDraft 削除は Voucher 化されないことが確定するため、
    # StorageUsage から減算する (file_size NULL のレガシーは対象外)。
    owner_id = draft.user_id
    size_to_release = draft.file_size or 0
    db.session.delete(draft)
    db.session.commit()
    storage = get_storage_backend()
    storage.delete(image_key)
    storage.delete(make_thumbnail_key(image_key))
    if size_to_release > 0:
        owner = db.session.get(User, owner_id)
        if owner is not None:
            record_delete(owner, size_to_release)
    flash("下書きを削除しました。", "info")
    return redirect(url_for("ai_journal.drafts"))


@bp.route("/drafts/<int:draft_id>/review", methods=["GET"])
@login_required
def drafts_review(draft_id):
    """ドラフトからreview画面へ遷移"""
    draft = AIDraft.query.get_or_404(draft_id)
    if draft.user_id != get_effective_user_id():
        flash("権限がありません。", "danger")
        return redirect(url_for("ai_journal.drafts"))

    if not draft.suggestions_json:
        flash("解析データがありません。", "warning")
        return redirect(url_for("ai_journal.drafts"))

    session["ai_journal_draft_id"] = draft.id

    idx = request.args.get("idx", 0, type=int)
    return redirect(url_for("ai_journal.review", idx=idx))


# --- review ---

@bp.route("/review", methods=["GET"])
@login_required
def review():
    """仕訳案の確認・編集画面を表示する（登録はクライアント暗号化経由）"""
    draft_id = session.get("ai_journal_draft_id")
    draft = None
    if draft_id:
        draft = AIDraft.query.get(draft_id)
        if draft and draft.user_id != get_effective_user_id():
            draft = None

    if not draft or not draft.suggestions_json:
        session.pop("ai_journal_draft_id", None)
        flash("AI解析データがありません。もう一度アップロードしてください。", "warning")
        return redirect(url_for("ai_journal.upload"))

    suggestions = json.loads(draft.suggestions_json)
    suggestion_index = request.args.get("idx", 0, type=int)
    if suggestion_index < 0 or suggestion_index >= len(suggestions):
        suggestion_index = 0

    selected = suggestions[suggestion_index]

    user_id = get_effective_user_id()
    grouped_accounts = get_grouped_accounts(user_id)
    closed_periods = get_closed_periods_map(user_id)
    restricted_before = get_restricted_before_year(user_id)
    is_saved_draft = draft.status == "analyzed"

    # 入力期限チェック
    deadline_exceeded = False
    sel_date = selected.get("date")
    if sel_date and draft.created_at:
        try:
            deadline_exceeded = check_deadline(
                date_type.fromisoformat(sel_date), draft.created_at,
            )
        except ValueError:
            pass

    return render_template(
        "ai_journal/review.html",
        suggestions=suggestions,
        selected=selected,
        selected_index=suggestion_index,
        grouped_accounts=grouped_accounts,
        draft_id=is_saved_draft,
        draft_db_id=draft.id,
        is_saved_draft=is_saved_draft,
        closed_periods=closed_periods,
        restricted_before_year=restricted_before,
        deadline_exceeded=deadline_exceeded,
    )
