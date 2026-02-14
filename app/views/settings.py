from datetime import date, datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models.user import User
from app.services.audit import get_effective_user_id
from app.models.account import Account, AccountType
from app.models.webauthn import WebAuthnCredential
from app.models.ai_config import UserAIConfig
from app.models.audit import AuditGrant, AuditGrantAccount
from app.services.ai_receipt import (
    encrypt_api_key, PROVIDER_DEFAULTS, PROVIDER_LABELS,
)
from app.views.accounts import TAX_CATEGORIES
from app.services.fiscal import (
    PERIOD_LABELS, get_closed_period, close_period, reopen_period,
)

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/passkeys")
@login_required
def passkeys():
    """Passkey 管理ページ"""
    credentials = (
        WebAuthnCredential.query.filter_by(user_id=current_user.id)
        .order_by(WebAuthnCredential.created_at.desc())
        .all()
    )
    return render_template("settings/passkeys.html", credentials=credentials)


@bp.route("/passkeys/<int:credential_id>/delete", methods=["POST"])
@login_required
def delete_passkey(credential_id):
    """Passkey 削除"""
    credential = WebAuthnCredential.query.filter_by(
        id=credential_id, user_id=current_user.id
    ).first_or_404()
    name = credential.name or f"Passkey #{credential.id}"
    db.session.delete(credential)
    db.session.commit()
    flash(f"Passkey「{name}」を削除しました。", "success")
    return redirect(url_for("settings.passkeys"))


# --- AI API 設定 ---


@bp.route("/ai")
@login_required
def ai_config():
    """AI API設定ページ"""
    config = UserAIConfig.query.filter_by(user_id=current_user.id).first()
    return render_template(
        "settings/ai_config.html",
        config=config,
        provider_defaults=PROVIDER_DEFAULTS,
        provider_labels=PROVIDER_LABELS,
    )


@bp.route("/ai/save", methods=["POST"])
@login_required
def ai_config_save():
    """AI API設定の保存"""
    provider = request.form.get("provider", "openai")
    api_key = request.form.get("api_key", "").strip()
    model_name = request.form.get("model_name", "").strip()

    if provider not in PROVIDER_DEFAULTS:
        flash("無効なプロバイダーです。", "danger")
        return redirect(url_for("settings.ai_config"))

    config = UserAIConfig.query.filter_by(user_id=current_user.id).first()

    if config:
        config.provider = provider
        config.model_name = model_name
        if api_key:
            config.api_key_encrypted = encrypt_api_key(api_key)
    else:
        if not api_key:
            flash("APIキーを入力してください。", "danger")
            return redirect(url_for("settings.ai_config"))
        config = UserAIConfig(
            user_id=current_user.id,
            provider=provider,
            api_key_encrypted=encrypt_api_key(api_key),
            model_name=model_name,
        )
        db.session.add(config)

    db.session.commit()
    flash("AI API設定を保存しました。", "success")
    return redirect(url_for("settings.ai_config"))


@bp.route("/ai/delete", methods=["POST"])
@login_required
def ai_config_delete():
    """AI API設定の削除"""
    config = UserAIConfig.query.filter_by(user_id=current_user.id).first()
    if config:
        db.session.delete(config)
        db.session.commit()
        flash("AI API設定を削除しました。", "success")
    return redirect(url_for("settings.ai_config"))


# --- 月次確定 ---


@bp.route("/fiscal")
@login_required
def fiscal():
    """月次確定管理ページ"""
    year = request.args.get("year", date.today().year, type=int)
    closed = get_closed_period(get_effective_user_id(), year)

    periods = []
    for p in range(0, 16):
        periods.append({
            "number": p,
            "label": PERIOD_LABELS[p],
            "closed": p <= closed,
            "can_close": p == closed + 1,
            "can_reopen": p == closed,
        })

    return render_template(
        "settings/fiscal.html",
        year=year,
        periods=periods,
        closed_period=closed,
    )


@bp.route("/fiscal/close", methods=["POST"])
@login_required
def fiscal_close():
    """月次確定を実行"""
    year = request.form.get("year", type=int)
    period = request.form.get("period", type=int)

    err = close_period(get_effective_user_id(), year, period)
    if err:
        flash(err, "danger")
    else:
        label = PERIOD_LABELS.get(period, f"{period}月")
        flash(f"{year}年{label}を確定しました。", "success")

    return redirect(url_for("settings.fiscal", year=year))


@bp.route("/fiscal/reopen", methods=["POST"])
@login_required
def fiscal_reopen():
    """月次確定を解除"""
    year = request.form.get("year", type=int)
    period = request.form.get("period", type=int)

    err = reopen_period(get_effective_user_id(), year, period)
    if err:
        flash(err, "danger")
    else:
        label = PERIOD_LABELS.get(period, f"{period}月")
        flash(f"{year}年{label}の確定を解除しました。", "success")

    return redirect(url_for("settings.fiscal", year=year))


# --- 監査アクセス管理 ---


PERMISSION_LABELS = {1: "Lv1: 集計結果のみ", 2: "Lv2: 税務科目のみ", 3: "Lv3: 本人同等"}


@bp.route("/audit")
@login_required
def audit():
    """監査アクセス管理ページ（個人ユーザー専用）"""
    if current_user.user_type != "personal":
        flash("この機能は個人ユーザー専用です。", "warning")
        return redirect(url_for("dashboard.index"))

    grants = (
        AuditGrant.query
        .filter_by(owner_user_id=current_user.id)
        .order_by(AuditGrant.created_at.desc())
        .all()
    )
    return render_template(
        "settings/audit_grants.html",
        grants=grants,
        permission_labels=PERMISSION_LABELS,
    )


@bp.route("/audit/add", methods=["POST"])
@login_required
def audit_add():
    """監査アクセスの付与"""
    if current_user.user_type != "personal":
        flash("この機能は個人ユーザー専用です。", "warning")
        return redirect(url_for("dashboard.index"))

    username = request.form.get("username", "").strip()
    level = request.form.get("permission_level", 1, type=int)

    if level not in (1, 2, 3):
        flash("無効な権限レベルです。", "danger")
        return redirect(url_for("settings.audit"))

    auditor = User.query.filter_by(username=username, user_type="auditor").first()
    if not auditor:
        flash(f"監査用アカウント「{username}」が見つかりません。", "danger")
        return redirect(url_for("settings.audit"))

    if auditor.id == current_user.id:
        flash("自分自身にはアクセスを付与できません。", "danger")
        return redirect(url_for("settings.audit"))

    existing = AuditGrant.query.filter_by(
        owner_user_id=current_user.id, auditor_user_id=auditor.id
    ).first()
    if existing:
        flash(f"「{username}」には既にアクセスが付与されています。", "warning")
        return redirect(url_for("settings.audit"))

    grant = AuditGrant(
        owner_user_id=current_user.id,
        auditor_user_id=auditor.id,
        permission_level=level,
    )
    db.session.add(grant)
    db.session.flush()

    # Lv2: tax_category付き科目 + 事業主 をデフォルト公開
    if level == 2:
        accounts = Account.query.filter_by(
            user_id=current_user.id, is_active=True
        ).all()
        for account in accounts:
            if account.tax_category or account.system_role == "proprietor":
                db.session.add(AuditGrantAccount(
                    audit_grant_id=grant.id,
                    account_id=account.id,
                ))

    db.session.commit()
    flash(f"「{username}」に{PERMISSION_LABELS[level]}のアクセスを付与しました。", "success")
    return redirect(url_for("settings.audit"))


@bp.route("/audit/<int:grant_id>/delete", methods=["POST"])
@login_required
def audit_delete(grant_id):
    """監査アクセスの取消"""
    grant = AuditGrant.query.filter_by(
        id=grant_id, owner_user_id=current_user.id
    ).first_or_404()
    auditor_name = grant.auditor.username
    db.session.delete(grant)
    db.session.commit()
    flash(f"「{auditor_name}」のアクセスを取り消しました。", "success")
    return redirect(url_for("settings.audit"))


@bp.route("/audit/<int:grant_id>/submit", methods=["POST"])
@login_required
def audit_submit(grant_id):
    """Lv2: 提出"""
    grant = AuditGrant.query.filter_by(
        id=grant_id, owner_user_id=current_user.id, permission_level=2
    ).first_or_404()
    grant.status = "submitted"
    grant.submitted_at = datetime.now(timezone.utc)
    db.session.commit()
    flash(f"「{grant.auditor.username}」に提出しました。", "success")
    return redirect(url_for("settings.audit"))


@bp.route("/audit/<int:grant_id>/unsubmit", methods=["POST"])
@login_required
def audit_unsubmit(grant_id):
    """Lv2: 提出取消"""
    grant = AuditGrant.query.filter_by(
        id=grant_id, owner_user_id=current_user.id, permission_level=2
    ).first_or_404()
    grant.status = "draft"
    grant.submitted_at = None
    db.session.commit()
    flash(f"「{grant.auditor.username}」への提出を取り消しました。", "success")
    return redirect(url_for("settings.audit"))


@bp.route("/audit/<int:grant_id>/accounts")
@login_required
def audit_accounts(grant_id):
    """Lv2用: 公開科目の管理画面"""
    grant = AuditGrant.query.filter_by(
        id=grant_id, owner_user_id=current_user.id, permission_level=2
    ).first_or_404()

    account_types = AccountType.query.order_by(AccountType.display_order).all()
    accounts = (
        Account.query
        .filter_by(user_id=current_user.id, is_active=True)
        .order_by(Account.code)
        .all()
    )

    published_ids = {
        ga.account_id for ga in grant.grant_accounts
    }

    tax_category_labels = {k: v for k, v in TAX_CATEGORIES if k}

    return render_template(
        "settings/audit_accounts.html",
        grant=grant,
        account_types=account_types,
        accounts=accounts,
        published_ids=published_ids,
        permission_labels=PERMISSION_LABELS,
        tax_category_labels=tax_category_labels,
    )


@bp.route("/audit/<int:grant_id>/accounts", methods=["POST"])
@login_required
def audit_accounts_save(grant_id):
    """公開科目の保存"""
    grant = AuditGrant.query.filter_by(
        id=grant_id, owner_user_id=current_user.id, permission_level=2
    ).first_or_404()

    if grant.status == "submitted":
        flash("提出済みのため公開科目を変更できません。", "danger")
        return redirect(url_for("settings.audit_accounts", grant_id=grant_id))

    selected_ids = set(request.form.getlist("account_ids", type=int))

    # 事業主は常に含める
    proprietor = Account.query.filter_by(
        user_id=current_user.id, system_role="proprietor"
    ).first()
    if proprietor:
        selected_ids.add(proprietor.id)

    # 既存をクリアして再作成
    AuditGrantAccount.query.filter_by(audit_grant_id=grant.id).delete()
    for aid in selected_ids:
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id,
            account_id=aid,
        ))

    db.session.commit()
    flash("公開科目を保存しました。", "success")
    return redirect(url_for("settings.audit_accounts", grant_id=grant_id))
