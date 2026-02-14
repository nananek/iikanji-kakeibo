from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models.webauthn import WebAuthnCredential
from app.models.ai_config import UserAIConfig
from app.services.ai_receipt import (
    encrypt_api_key, PROVIDER_DEFAULTS, PROVIDER_LABELS,
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
