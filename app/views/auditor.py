from flask import Blueprint, render_template, redirect, url_for, flash, session
from flask_login import login_required, current_user

from app.models.audit import AuditGrant

bp = Blueprint("auditor", __name__, url_prefix="/auditor")

PERMISSION_LABELS = {1: "Lv1: 集計結果のみ", 2: "Lv2: 税務科目のみ", 3: "Lv3: 本人同等"}


@bp.route("/")
@login_required
def dashboard():
    """クライアント一覧"""
    if current_user.user_type != "auditor":
        return redirect(url_for("dashboard.index"))

    grants = (
        AuditGrant.query
        .filter_by(auditor_user_id=current_user.id)
        .order_by(AuditGrant.created_at.desc())
        .all()
    )
    return render_template(
        "auditor/dashboard.html",
        grants=grants,
        permission_labels=PERMISSION_LABELS,
    )


@bp.route("/packages/<int:grant_id>")
@login_required
def packages(grant_id):
    """受信した監査スナップショットの閲覧ページ (auditor 側, §14.5)。

    HPKE 復号・スナップショット表示・修正案送信はすべてクライアント
    (audit/audit_review_renderer) が行う。本ビューは描画に必要なメタ (grant /
    owner) を JSON island で渡すだけ。owner の平文帳簿はサーバに渡らない。
    """
    if current_user.user_type != "auditor":
        return redirect(url_for("dashboard.index"))

    grant = AuditGrant.query.filter_by(
        id=grant_id, auditor_user_id=current_user.id
    ).first_or_404()
    # 失効した監査アクセスは新規閲覧を拒否する (§14.10、owner 側送信ビューと対称)。
    if grant.revoked_at is not None:
        flash("この監査アクセスは失効しています。", "warning")
        return redirect(url_for("auditor.dashboard"))

    return render_template(
        "auditor/packages.html",
        grant=grant,
        permission_labels=PERMISSION_LABELS,
    )


@bp.route("/switch/<int:grant_id>", methods=["POST"])
@login_required
def switch(grant_id):
    """クライアント切替"""
    grant = AuditGrant.query.filter_by(
        id=grant_id, auditor_user_id=current_user.id
    ).first_or_404()

    # Lv2は提出済みでないと切替不可
    if grant.permission_level == 2 and grant.status != "submitted":
        flash("未提出のため切り替えできません。", "warning")
        return redirect(url_for("auditor.dashboard"))

    session["acting_as_user_id"] = grant.owner_user_id
    session["acting_as_permission_level"] = grant.permission_level
    flash(f"「{grant.owner.username}」のデータを閲覧中です。", "info")

    if grant.permission_level == 1:
        return redirect(url_for("reports.tax"))
    return redirect(url_for("dashboard.index"))


@bp.route("/exit", methods=["POST"])
@login_required
def exit_acting():
    """代理閲覧終了"""
    session.pop("acting_as_user_id", None)
    session.pop("acting_as_permission_level", None)
    flash("代理閲覧を終了しました。", "info")
    return redirect(url_for("auditor.dashboard"))
