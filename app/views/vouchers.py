"""証憑一覧ビュー — 電帳法検索要件対応"""

import hashlib

from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify
from flask_login import login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.models.ai_config import UserAIConfig
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.audit import get_effective_user_id
from app.services.storage import get_storage_backend
from app.services.storage_quota import QuotaExceededError
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


@bp.route("/<int:voucher_id>/verify", methods=["POST"])
@login_required
def verify(voucher_id):
    """証憑ハッシュ検証"""
    user_id = get_effective_user_id()
    voucher = Voucher.query.filter_by(id=voucher_id, user_id=user_id).first_or_404()

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
def attach(entry_id):
    """AJAX: 既存仕訳に証憑画像を添付する"""
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
        return jsonify({"error": str(exc)}), 413

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
