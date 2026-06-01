"""証憑一覧ビュー — 電帳法検索要件対応"""

import hashlib
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, flash, redirect, url_for, session,
)
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload

from app.extensions import db, limiter
from app.models.user import User
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.services.audit import get_effective_user_id
from app.services.storage import get_storage_backend, make_thumbnail_key
from app.services.storage_quota import record_delete

bp = Blueprint("vouchers", __name__, url_prefix="/vouchers")


@bp.route("/")
@login_required
def index():
    """証憑一覧（E3-F PR-D-4-4 でクライアント描画に移行）。

    電帳法 検索要件 (日付・金額・摘要) のうち、日付/摘要/金額はいずれも紐付け仕訳の
    平文 (JournalEntry.date/description, JournalEntryLine.debit_amount 合計) に
    依存していた。E2EE 化で平文列を DROP するため、仕訳由来の表示・検索を
    クライアント復号描画に移す。サーバ shell は証憑の非暗号化メタのみを渡す
    (id / journal_entry_id / entry_number / fiscal_year / uploaded_at / has_hash)。
    クライアントが紐付け仕訳を MK 復号して日付・摘要・金額を補完し、検索も
    クライアント側で行う。サーバ側で entry.date / description / total_debit は
    一切読まない (entry_number / fiscal_year は DROP 対象外の平文メタ)。
    """
    user_id = get_effective_user_id()

    vouchers = (
        Voucher.active()
        .options(joinedload(Voucher.journal_entry))
        .filter(Voucher.user_id == user_id)
        .all()
    )
    voucher_meta = [
        {
            "id": v.id,
            "journal_entry_id": v.journal_entry_id,
            "entry_number": v.journal_entry.entry_number if v.journal_entry else None,
            "fiscal_year": v.journal_entry.fiscal_year if v.journal_entry else None,
            "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
            "has_hash": bool(v.file_hash),
            # E4 PR-C2: 暗号化証憑 (encrypted_meta_blob あり) はクライアントが
            # fetch + 復号して表示する。レガシー平文証憑は従来通りサーバ配信。
            "encrypted": v.encrypted_meta_blob is not None,
        }
        for v in vouchers
    ]

    # 削除ボタンは本人モード時のみ表示 (代理閲覧中の auditor は破壊操作禁止)
    can_delete = session.get("acting_as_user_id") is None

    return render_template(
        "vouchers/index.html",
        voucher_meta=voucher_meta,
        effective_user_id=user_id,
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


@bp.route("/<int:voucher_id>/delete", methods=["POST"])
@login_required
@limiter.limit("10/minute", methods=["POST"])
def delete(voucher_id):
    """証憑削除 (Phase 5 #70 / 電帳法証跡永続化)。

    Voucher は論理削除 (`deleted_at` セット) する。物理 row は DB に
    残るため `VoucherAuditLog` の FK RESTRICT 制約と共存でき、
    `action="deleted"` の削除証跡を **DB に永続化** できる (電帳法
    スキャナ保存「訂正削除の事実と内容を確認できること」要件に対応)。

    ストレージ上の画像ファイルは即削除する (容量解放を優先)。訂正削除
    の事実と内容は `action="deleted"` の AuditLog + 論理削除で残る voucher
    行の各列 (image_key / file_hash(cipher) / file_size) で担保する
    (E4 PR-D で平文 detail への file_hash 記録は廃止済)。

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
    # E4 PR-D: 平文 detail は書かない。「何が削除されたか」(image_key /
    # file_hash(cipher) / file_size) は論理削除後も残る voucher 行の各列に
    # 保持されており冗長 (画像本体のみストレージから削除、行は残す)。
    # action="deleted" + created_at + 持続行で訂正削除の事実と内容を確認できる。
    db.session.add(VoucherAuditLog(
        voucher_id=voucher.id,
        user_id=user_id,
        action="deleted",
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
