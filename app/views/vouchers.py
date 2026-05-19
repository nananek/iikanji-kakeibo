"""証憑一覧ビュー — 電帳法検索要件対応"""

import hashlib
import json
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, request, flash, redirect, url_for,
    jsonify, session,
)
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.extensions import db, limiter
from app.models.user import User
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.models.ai_config import UserAIConfig
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.audit import get_effective_user_id
from app.services.storage import get_storage_backend, make_thumbnail_key
from app.services.storage_quota import QuotaExceededError, record_delete
from app.services.voucher import create_voucher_from_upload

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

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

    # 論理削除 (Phase 5 #70 電帳法証跡永続化) されたものを除外
    query = (
        Voucher.active()
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

    # 削除ボタンは本人モード時のみ表示 (代理閲覧中の auditor は破壊操作禁止)
    can_delete = session.get("acting_as_user_id") is None

    return render_template(
        "vouchers/index.html",
        vouchers=vouchers,
        date_from=date_from,
        date_to=date_to,
        amount_from=amount_from,
        amount_to=amount_to,
        search=search,
        can_delete=can_delete,
    )


@bp.route("/<int:voucher_id>/verify", methods=["POST"])
@login_required
def verify(voucher_id):
    """証憑ハッシュ検証"""
    user_id = get_effective_user_id()
    voucher = Voucher.active().filter_by(
        id=voucher_id, user_id=user_id,
    ).first_or_404()

    if not voucher.file_hash:
        flash("この証憑にはハッシュが記録されていません。", "warning")
        return redirect(url_for("vouchers.index"))

    try:
        image_data = get_storage_backend().get(voucher.image_key)
    except FileNotFoundError:
        flash("証憑画像がストレージに見つかりません。", "danger")
        return redirect(url_for("vouchers.index"))

    computed = hashlib.sha256(image_data).hexdigest()
    verified = computed == voucher.file_hash

    db.session.add(VoucherAuditLog(
        voucher_id=voucher.id,
        user_id=user_id,
        action="hash_verified" if verified else "hash_mismatch",
    ))
    db.session.commit()

    if verified:
        flash("ハッシュ検証に成功しました。証憑は改ざんされていません。", "success")
    else:
        flash("ハッシュ不一致！証憑が改ざんされている可能性があります。", "danger")
    return redirect(url_for("vouchers.index"))


@bp.route("/attach/<int:entry_id>", methods=["POST"])
@login_required
@limiter.limit("10/minute", methods=["POST"])
def attach(entry_id):
    """AJAX: 既存仕訳に証憑画像を添付する。

    レート制限 (`10/minute`) は他 upload 系 (csv_import / ofx_import 等)
    と同水準。quota 統合 (Phase 5 #70) により毎リクエストで DB アクセス
    が増えるため、DoS 的な連打を抑止する目的でも必要。

    末尾の `db.session.commit()` は `record_upload` 内で commit 済の
    トランザクションに対する冪等な操作 (補助的に AI 解析結果 etc を
    保存するため)。`create_voucher_from_upload` の責任で容量計上と
    Voucher 永続化は完了済。
    """
    user_id = get_effective_user_id()
    entry = JournalEntry.query.filter_by(
        id=entry_id, user_id=user_id,
    ).first()
    if not entry:
        return jsonify({"error": "仕訳が見つかりません。"}), 404

    image_file = request.files.get("image")
    if not image_file or not image_file.filename:
        return jsonify({"error": "画像ファイルを選択してください。"}), 400

    image_bytes = image_file.read()
    if len(image_bytes) > MAX_IMAGE_SIZE:
        return jsonify({"error": "ファイルサイズが大きすぎます（上限10MB）。"}), 400

    mime_type = image_file.content_type
    if mime_type not in ALLOWED_MIME_TYPES:
        return jsonify({
            "error": "対応していないファイル形式です。JPEG/PNG/WebP/GIF を使用してください。",
        }), 400

    try:
        voucher = create_voucher_from_upload(
            user_id=user_id,
            journal_entry_id=entry.id,
            image_bytes=image_bytes,
            mime_type=mime_type,
            original_filename=image_file.filename,
        )
    except QuotaExceededError as exc:
        # CodeQL py/stack-trace-exposure 誤検出対策で `str(exc)` ではなく
        # 明示的に `user_message` 属性経由でユーザー向け固定文言を返す。
        return jsonify({"error": exc.user_message}), 413

    response_data = {"ok": True, "voucher_id": voucher.id}

    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    if config and config.api_key_encrypted:
        try:
            from app.services.ai_receipt import analyze_voucher_for_attachment

            result = analyze_voucher_for_attachment(
                user_id=user_id,
                image_bytes=image_bytes,
                mime_type=mime_type,
                journal_date=entry.date.isoformat(),
                journal_amount=int(sum(
                    line.debit_amount for line in entry.lines
                )),
                journal_description=entry.description or "",
            )
            if result.get("compliance"):
                response_data["compliance"] = result["compliance"]
            response_data["consistency"] = result["consistency"]
        except Exception as e:
            response_data["ai_error"] = str(e)

    db.session.commit()
    return jsonify(response_data)


@bp.route("/<int:voucher_id>/delete", methods=["POST"])
@login_required
@limiter.limit("10/minute", methods=["POST"])
def delete(voucher_id):
    """証憑削除 (Phase 5 #70 / 電帳法証跡永続化)。

    Voucher は論理削除 (`deleted_at` セット) する。物理 row は DB に
    残るため `VoucherAuditLog` の FK RESTRICT 制約と共存でき、
    `action="deleted"` の削除証跡を **DB に永続化** できる (電帳法
    スキャナ保存「訂正削除の事実と内容を確認できること」要件に対応)。

    ストレージ上の画像ファイルは即削除する (容量解放を優先、訂正削除
    の事実は AuditLog の `detail` に file_hash を残すことで担保)。

    代理閲覧中 (`acting_as_user_id` セッション設定) は破壊操作を禁止
    (auditor は閲覧者であり、本人の意思によらない削除は監査独立性を
    損なう)。本人ログイン時のみ操作可能。
    """
    if session.get("acting_as_user_id") is not None:
        flash("代理閲覧中は証憑を削除できません。", "danger")
        return redirect(url_for("vouchers.index"))

    user_id = current_user.id
    voucher = Voucher.active().filter_by(
        id=voucher_id, user_id=user_id,
    ).first_or_404()

    image_key = voucher.image_key
    size_to_release = voucher.file_size or 0
    file_hash = voucher.file_hash or ""

    from flask import current_app
    # 電帳法の訂正削除証跡を DB に永続化 (`action="deleted"` の AuditLog)。
    # detail に画像 key / hash / サイズを格納し、ファイル本体が削除されて
    # も「何が削除されたか」を後から検証可能にする。
    db.session.add(VoucherAuditLog(
        voucher_id=voucher.id,
        user_id=user_id,
        action="deleted",
        detail=json.dumps({
            "image_key": image_key,
            "file_hash": file_hash,
            "file_size": size_to_release,
        }, ensure_ascii=False),
    ))
    # 論理削除: deleted_at を立てる。物理 row は残るため FK RESTRICT も
    # 満たす。`Voucher.active()` 経由の query から透過的に除外される。
    voucher.deleted_at = datetime.now(timezone.utc)
    # 運用フィルタ用に warning ログも残す (DB と二重で検出可能)
    current_app.logger.warning(
        "voucher deleted: id=%d user_id=%d image_key=%s file_hash=%s",
        voucher.id, user_id, image_key, file_hash,
    )
    db.session.commit()

    # ストレージ削除 (best-effort)。画像ファイル本体は容量解放のため即削除。
    storage = get_storage_backend()
    for k in (image_key, make_thumbnail_key(image_key)):
        try:
            storage.delete(k)
        except Exception as e:
            current_app.logger.warning(
                "voucher delete: storage delete failed %s: %s", k, e,
            )

    # 容量解放 (best-effort)。Voucher 論理削除は既に完了しているため、
    # record_delete の例外で HTTP 500 を返してもユーザー混乱を招く。
    # 失敗はログに残し、整合性監査バッチで StorageUsage drift を補完。
    if size_to_release > 0:
        owner = db.session.get(User, user_id)
        if owner is not None:
            try:
                record_delete(owner, size_to_release)
            except Exception as e:
                current_app.logger.exception(
                    "voucher delete: record_delete failed (user=%d size=%d): %s",
                    user_id, size_to_release, e,
                )

    flash("証憑を削除しました。", "info")
    return redirect(url_for("vouchers.index"))
