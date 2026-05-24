"""AI証憑仕訳ビュー"""

import hashlib
import json
from dataclasses import asdict
from datetime import date as date_type

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, session,
    jsonify, make_response,
)
from flask_login import login_required, current_user

from app.extensions import db
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.user import User
from app.services.audit import get_effective_user_id, get_submitted_account_codes
from app.services.ai_receipt import analyze_and_suggest
from app.services.accounting import create_journal_entry
from app.services.fiscal import check_period_open_for_new, get_closed_periods_map, get_restricted_before_year
from app.services.image import serve_image
from app.services.storage import (
    get_storage_backend, make_storage_key, make_thumbnail_key,
    store_image_with_thumbnail,
)
from app.services.storage_quota import (
    QuotaExceededError, check_quota, get_quota_bytes, get_used_bytes,
    maybe_send_quota_warning, record_delete, record_upload,
)
from app.views.helpers import safe_user_error
from app.services.voucher import create_voucher_from_draft
from app.models.voucher import Voucher
from app.views.helpers import get_grouped_accounts, check_deadline

bp = Blueprint("ai_journal", __name__, url_prefix="/ai-journal")

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


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
    # 旧 Fernet データが残っているかも判定: migrate-key 呼出後は
    # api_key_encrypted=NULL になり、サーバ側 ai_receipt.py の解析が失敗する
    # ため、文言を「移行期間中=サーバ解析可」と「移行完了=解析不可」で分岐。
    config_is_e2ee = bool(config and config.is_e2ee)
    has_legacy_key = bool(config and config.api_key_encrypted)
    return render_template(
        "ai_journal/upload.html",
        has_config=bool(config),
        config_is_e2ee=config_is_e2ee,
        has_legacy_key=has_legacy_key,
        draft_count=draft_count,
    )


@bp.route("/analyze", methods=["POST"])
@login_required
def analyze():
    """AJAX: 証憑画像を解析して仕訳案を返す"""
    config = UserAIConfig.query.filter_by(user_id=get_effective_user_id()).first()
    if not config:
        return jsonify({"error": "AI API設定が登録されていません。先に設定してください。"}), 400
    # E2EE 移行完了済 (migrate-key 実行後で api_key_encrypted=NULL) では
    # サーバ側 ai_receipt.py が Fernet 復号できないため、サーバ経由解析は不可。
    # 通常 UI からは E2EE モードのフロー (POST /api/v1/ai/uploads など) に
    # 切り替わるためここに到達しない。直接 POST に対する安全網。
    if config.is_e2ee and not config.api_key_encrypted:
        return jsonify({
            "error": "E2EE 移行完了済のためサーバ経由解析は無効です。"
                     "UI から E2EE モードで解析するか、設定画面で API キーを"
                     "再登録してください。",
        }), 400

    image_file = request.files.get("image_file")
    if not image_file or not image_file.filename:
        return jsonify({"error": "画像ファイルを選択してください。"}), 400

    image_bytes = image_file.read()
    if len(image_bytes) > MAX_IMAGE_SIZE:
        return jsonify({"error": "ファイルサイズが大きすぎます（上限10MB）。"}), 400

    mime_type = image_file.content_type
    if mime_type not in ALLOWED_MIME_TYPES:
        return jsonify({
            "error": "対応していないファイル形式です。JPEG/PNG/WebP/GIF を使用してください。"
        }), 400

    # Phase 5 #70: AIDraft 段階で StorageUsage に計上する。Voucher 化時は
    # 所有権移転で計上不変、reject/期限切れ削除時に record_delete で減算。
    size = len(image_bytes)
    user_id = get_effective_user_id()
    owner = db.session.get(User, user_id)
    if owner is None:
        return jsonify({"error": "ユーザーが見つかりません。"}), 400
    try:
        check_quota(owner, size)
    except QuotaExceededError as exc:
        # CodeQL py/stack-trace-exposure 対策で `exc.user_message` 経由 (PR #92)
        return jsonify({"error": exc.user_message}), 413

    file_hash = hashlib.sha256(image_bytes).hexdigest()
    comment = request.form.get("comment", "").strip()

    try:
        suggestions = analyze_and_suggest(
            user_id, image_bytes, mime_type, comment=comment
        )
    except (ValueError, RuntimeError) as e:
        from flask import current_app
        current_app.logger.exception("analyze_and_suggest failed")
        return jsonify({"error": safe_user_error(e)}), 400

    suggestions_data = [asdict(s) for s in suggestions]
    suggestions_json = json.dumps(suggestions_data, ensure_ascii=False)

    # 画像・解析結果を保存（セッションには小さなIDのみ）
    draft = AIDraft(
        user_id=user_id,
        image_key="",
        image_mime=mime_type,
        file_hash=file_hash,
        file_size=size,
        comment=comment,
        suggestions_json=suggestions_json,
        status="temp",
    )
    db.session.add(draft)
    db.session.flush()
    key = make_storage_key(draft.user_id, draft.id, mime_type)
    store_image_with_thumbnail(key, image_bytes, mime_type)
    draft.image_key = key
    db.session.commit()

    # 容量加算 (ON CONFLICT upsert) + TOCTOU 楽観的再検証。
    # create_voucher_from_upload と同じパターン: 並行アップロードで合算が
    # 上限超過なら巻き戻し (ストレージ削除 + draft 削除 + record_delete)。
    # Draft は既に commit 済のため、record_upload の例外で 500 を返すと
    # 「Draft は永続化されたのに容量計上されない」quota リークが発生する。
    # 明示的に握ってログに残し、整合性監査バッチで補完する設計とする。
    record_upload_succeeded = False
    try:
        record_upload(owner, size)
        record_upload_succeeded = True
    except Exception as e:
        from flask import current_app
        current_app.logger.exception(
            "ai_journal: record_upload failed (user=%d size=%d): %s",
            owner.id, size, e,
        )
    # record_upload が失敗した場合、StorageUsage には加算されていないため
    # TOCTOU 検証をスキップする。これをやらないと、別ユーザーが先に上限近く
    # まで埋めた状態で当該リクエストが超過判定 → record_delete で他ユーザー
    # の計上を誤減算する経路ができてしまう。
    if record_upload_succeeded and get_used_bytes(owner) > get_quota_bytes(owner):
        from flask import current_app
        storage = get_storage_backend()
        for k in (key, make_thumbnail_key(key)):
            try:
                storage.delete(k)
            except Exception as e:
                current_app.logger.warning(
                    "ai_journal rollback: storage delete failed %s: %s", k, e,
                )
        db.session.delete(draft)
        db.session.commit()
        try:
            record_delete(owner, size)
        except Exception as e:
            current_app.logger.exception(
                "ai_journal rollback: record_delete failed (user=%d size=%d): %s",
                owner.id, size, e,
            )
        return jsonify({
            "error": "並行アップロードにより容量上限を超えました。再試行してください。",
        }), 413

    session["ai_journal_draft_id"] = draft.id

    # 容量警告メール送信 (Phase 6 #71)。閾値超過時のみ、失敗は best-effort
    maybe_send_quota_warning(owner)

    return jsonify({"suggestions": suggestions_data})


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

    # 各ドラフトのサマリーを抽出
    draft_list = []
    for d in items:
        summary = {}
        if d.suggestions_json:
            try:
                suggestions = json.loads(d.suggestions_json)
                if suggestions:
                    s = suggestions[0]
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
        draft_list.append({"draft": d, "summary": summary})

    return render_template("ai_journal/drafts.html", draft_list=draft_list)


@bp.route("/drafts/<int:draft_id>/image")
@login_required
def draft_image(draft_id):
    """ドラフトの画像を返す"""
    draft = AIDraft.query.get_or_404(draft_id)
    if draft.user_id != get_effective_user_id():
        return "", 403
    try:
        return serve_image(draft.image_key, draft.image_mime, draft.file_hash)
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
        return serve_image(voucher.image_key, voucher.image_mime, voucher.file_hash)
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


@bp.route("/drafts/<int:draft_id>/quick-accept", methods=["POST"])
@login_required
def drafts_quick_accept(draft_id):
    """案1をそのまま仕訳登録する"""
    is_hx = request.headers.get("HX-Request")

    def _hx_error(msg, redirect_endpoint="ai_journal.drafts", **redirect_kwargs):
        if is_hx:
            resp = make_response("", 422)
            resp.headers["HX-Reswap"] = "none"
            resp.headers["HX-Trigger"] = json.dumps(
                {"showToast": {"message": msg, "type": "danger"}}
            )
            return resp
        flash(msg, "danger")
        return redirect(url_for(redirect_endpoint, **redirect_kwargs))

    draft = AIDraft.query.get_or_404(draft_id)
    user_id = get_effective_user_id()
    if draft.user_id != user_id:
        return _hx_error("権限がありません。")

    if draft.status == "done":
        return _hx_error("この下書きは仕訳登録済みです。")

    if not draft.suggestions_json:
        return _hx_error("解析データがありません。")

    try:
        suggestions = json.loads(draft.suggestions_json)
    except (json.JSONDecodeError, IndexError):
        return _hx_error("解析データが不正です。")

    if not suggestions:
        return _hx_error("仕訳案がありません。")

    selected = suggestions[0]
    entry_date_str = selected.get("date", "")
    description = selected.get("entry_description", "").strip()

    if not entry_date_str or not description:
        return _hx_error(
            "案1の日付または摘要が不足しています。レビュー画面で確認してください。",
            "ai_journal.drafts_review", draft_id=draft_id,
        )

    try:
        entry_date = date_type.fromisoformat(entry_date_str)
    except ValueError:
        return _hx_error(
            "案1の日付が不正です。レビュー画面で確認してください。",
            "ai_journal.drafts_review", draft_id=draft_id,
        )

    lines_data = [
        {
            "account_code": line["account_code"],
            "debit_amount": int(line.get("debit_amount", 0) or 0),
            "credit_amount": int(line.get("credit_amount", 0) or 0),
            "description": line.get("description", ""),
        }
        for line in selected.get("lines", [])
        if line.get("account_code")
    ]

    if not lines_data:
        return _hx_error(
            "案1に仕訳明細がありません。レビュー画面で確認してください。",
            "ai_journal.drafts_review", draft_id=draft_id,
        )

    # 提出済みロック科目チェック
    locked_codes = get_submitted_account_codes(user_id)
    if locked_codes:
        used_codes = {line["account_code"] for line in lines_data}
        if used_codes & locked_codes:
            return _hx_error(
                "提出済みの税務科目を含むため登録できません。",
                "ai_journal.drafts_review", draft_id=draft_id,
            )

    # 確定済み期間チェック
    err = check_period_open_for_new(user_id, entry_date.year, entry_date.month)
    if err:
        return _hx_error(err, "ai_journal.drafts_review", draft_id=draft_id)

    try:
        entry = create_journal_entry(
            user_id=user_id,
            date=entry_date,
            description=description,
            lines_data=lines_data,
            source="ai_receipt",
        )
        _update_discord_done(draft, entry.entry_number)
        create_voucher_from_draft(draft, entry.id)
        db.session.commit()
    except ValueError as e:
        return _hx_error(str(e), "ai_journal.drafts_review", draft_id=draft_id)

    msg = f"伝票 #{entry.entry_number} を登録しました。"
    if is_hx:
        resp = make_response("", 200)
        resp.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": msg, "type": "success"}}
        )
        return resp

    flash(msg, "success")
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

@bp.route("/review", methods=["GET", "POST"])
@login_required
def review():
    """仕訳案の確認・編集・保存"""
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

    suggestions_json = draft.suggestions_json

    suggestions = json.loads(suggestions_json)
    suggestion_index = request.args.get("idx", 0, type=int)
    if suggestion_index < 0 or suggestion_index >= len(suggestions):
        suggestion_index = 0

    selected = suggestions[suggestion_index]

    user_id = get_effective_user_id()
    grouped_accounts = get_grouped_accounts(user_id)
    closed_periods = get_closed_periods_map(user_id)
    restricted_before = get_restricted_before_year(user_id)
    is_saved_draft = draft.status == "analyzed"

    if request.method == "POST":
        mode = request.form.get("mode", "simple")
        entry_date_str = request.form.get("date", "")
        description = request.form.get("description", "").strip()

        if not entry_date_str or not description:
            flash("日付と摘要を入力してください。", "danger")
            return render_template(
                "ai_journal/review.html",
                suggestions=suggestions,
                selected=selected,
                selected_index=suggestion_index,
                grouped_accounts=grouped_accounts,
                draft_id=is_saved_draft,
                closed_periods=closed_periods,
                restricted_before_year=restricted_before,
            )

        try:
            entry_date = date_type.fromisoformat(entry_date_str)
        except ValueError:
            flash("日付の形式が不正です。", "danger")
            return render_template(
                "ai_journal/review.html",
                suggestions=suggestions,
                selected=selected,
                selected_index=suggestion_index,
                grouped_accounts=grouped_accounts,
                draft_id=is_saved_draft,
                closed_periods=closed_periods,
                restricted_before_year=restricted_before,
            )

        if mode == "simple":
            amount = request.form.get("amount", 0, type=int)
            category_code = request.form.get("category_account_code", "")
            pay_account_code = request.form.get(
                "payment_account_code", ""
            )

            if amount <= 0 or not category_code or not pay_account_code:
                flash("金額・費目・支払元を入力してください。", "danger")
                return render_template(
                    "ai_journal/review.html",
                    suggestions=suggestions,
                    selected=selected,
                    selected_index=suggestion_index,
                    grouped_accounts=grouped_accounts,
                    draft_id=is_saved_draft,
                    closed_periods=closed_periods,
                    restricted_before_year=restricted_before,
                )

            lines_data = [
                {
                    "account_code": category_code,
                    "debit_amount": amount,
                    "credit_amount": 0,
                    "description": "",
                },
                {
                    "account_code": pay_account_code,
                    "debit_amount": 0,
                    "credit_amount": amount,
                    "description": "",
                },
            ]
        else:
            lines_json = request.form.get("lines_json", "[]")
            try:
                raw_lines = json.loads(lines_json)
            except json.JSONDecodeError:
                flash("明細データが不正です。", "danger")
                return render_template(
                    "ai_journal/review.html",
                    suggestions=suggestions,
                    selected=selected,
                    selected_index=suggestion_index,
                    grouped_accounts=grouped_accounts,
                    draft_id=is_saved_draft,
                    closed_periods=closed_periods,
                    restricted_before_year=restricted_before,
                )

            lines_data = [
                {
                    "account_code": line["account_code"],
                    "debit_amount": int(line.get("debit_amount", 0) or 0),
                    "credit_amount": int(line.get("credit_amount", 0) or 0),
                    "description": line.get("description", ""),
                }
                for line in raw_lines
                if line.get("account_code")
            ]

        if not lines_data:
            flash("仕訳明細を入力してください。", "danger")
            return render_template(
                "ai_journal/review.html",
                suggestions=suggestions,
                selected=selected,
                selected_index=suggestion_index,
                grouped_accounts=grouped_accounts,
                draft_id=is_saved_draft,
                closed_periods=closed_periods,
                restricted_before_year=restricted_before,
            )

        # 提出済みロック科目チェック
        locked_codes = get_submitted_account_codes(get_effective_user_id())
        if locked_codes:
            used_codes = {line["account_code"] for line in lines_data}
            if used_codes & locked_codes:
                flash("提出済みの税務科目を含むため登録できません。", "danger")
                return render_template(
                    "ai_journal/review.html",
                    suggestions=suggestions,
                    selected=selected,
                    selected_index=suggestion_index,
                    grouped_accounts=grouped_accounts,
                    draft_id=is_saved_draft,
                    closed_periods=closed_periods,
                    restricted_before_year=restricted_before,
                )

        # 確定済み期間チェック
        err = check_period_open_for_new(
            get_effective_user_id(), entry_date.year, entry_date.month
        )
        if err:
            flash(err, "danger")
            return render_template(
                "ai_journal/review.html",
                suggestions=suggestions,
                selected=selected,
                selected_index=suggestion_index,
                grouped_accounts=grouped_accounts,
                draft_id=is_saved_draft,
                closed_periods=closed_periods,
                restricted_before_year=restricted_before,
            )

        try:
            entry = create_journal_entry(
                user_id=get_effective_user_id(),
                date=entry_date,
                description=description,
                lines_data=lines_data,
                source="ai_receipt",
            )
            session.pop("ai_journal_draft_id", None)
            _update_discord_done(draft, entry.entry_number)
            create_voucher_from_draft(draft, entry.id)
            db.session.commit()

            flash(f"伝票 #{entry.entry_number} を登録しました。", "success")

            if is_saved_draft:
                return redirect(url_for("ai_journal.drafts"))
            return redirect(url_for("journal.index"))
        except ValueError as e:
            flash(str(e), "danger")

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
        closed_periods=closed_periods,
        restricted_before_year=restricted_before,
        deadline_exceeded=deadline_exceeded,
    )


def _update_discord_done(draft, entry_number):
    """仕訳登録後に Discord 通知を完了マークに更新する"""
    if not draft.discord_message_id or not draft.discord_webhook_url:
        return
    from app.services.notify import update_discord_message

    original_desc = ""
    if draft.suggestions_json:
        try:
            suggestions = json.loads(draft.suggestions_json)
            if suggestions:
                s = suggestions[0]
                parts = []
                if s.get("date"):
                    parts.append(s["date"])
                if s.get("entry_description"):
                    parts.append(s["entry_description"])
                original_desc = " ".join(parts)
        except (json.JSONDecodeError, IndexError):
            pass

    msg = ""
    if original_desc:
        msg += f"~~{original_desc}~~\n"
    msg += f"仕訳を登録しました（伝票 #{entry_number}）"

    update_discord_message(
        webhook_url=draft.discord_webhook_url,
        message_id=draft.discord_message_id,
        title="✅ いいかんじ™家計簿 AI仕訳",
        message=msg,
    )
