from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from urllib.parse import urlparse

from app.extensions import db, limiter
from app.models.user import User
from app.forms.auth import LoginForm, RegisterForm
from app.services.seed import seed_accounts_for_user
from app.services.captcha import is_captcha_enabled, get_captcha_response_field, verify_captcha_token


def _check_captcha() -> bool:
    """CAPTCHA 検証。未設定時は常に True。"""
    if not is_captcha_enabled():
        return True
    field = get_captcha_response_field()
    token = request.form.get(field, "")
    if not token or not verify_captcha_token(token):
        flash("CAPTCHA認証に失敗しました。もう一度お試しください。", "danger")
        return False
    return True


def _safe_next_url(fallback):
    """next パラメータが内部 URL であれば返す。外部 URL は無視する。"""
    next_page = request.args.get("next")
    if next_page:
        parsed = urlparse(next_page)
        if not parsed.netloc and not parsed.scheme:
            return next_page
    return fallback

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    if form.validate_on_submit():
        if not _check_captcha():
            return render_template("auth/login.html", form=form)
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.user_type == "personal" and user.check_password(form.password.data):
            login_user(user, remember=True)
            return redirect(_safe_next_url(url_for("dashboard.index")))
        flash("ユーザー名またはパスワードが正しくありません。", "danger")

    return render_template("auth/login.html", form=form)


@bp.route("/login/auditor", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
def login_auditor():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    if form.validate_on_submit():
        if not _check_captcha():
            return render_template("auth/login_auditor.html", form=form)
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.user_type == "auditor" and user.check_password(form.password.data):
            login_user(user, remember=True)
            return redirect(_safe_next_url(url_for("auditor.dashboard")))
        flash("ユーザー名またはパスワードが正しくありません。", "danger")

    return render_template("auth/login_auditor.html", form=form)


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5/minute", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = RegisterForm()
    if form.validate_on_submit():
        if not _check_captcha():
            return render_template("auth/register.html", form=form)
        user = User(
            username=form.username.data,
            email=form.email.data,
            user_type="personal",
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()

        seed_accounts_for_user(user.id)

        flash("アカウントを作成しました。ログインしてください。", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html", form=form)


@bp.route("/register/auditor", methods=["GET", "POST"])
@limiter.limit("5/minute", methods=["POST"])
def register_auditor():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = RegisterForm()
    if form.validate_on_submit():
        if not _check_captcha():
            return render_template("auth/register_auditor.html", form=form)
        user = User(
            username=form.username.data,
            email=form.email.data,
            user_type="auditor",
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()

        flash("監査用アカウントを作成しました。ログインしてください。", "success")
        return redirect(url_for("auth.login_auditor"))

    return render_template("auth/register_auditor.html", form=form)


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("ログアウトしました。", "info")
    return redirect(url_for("auth.login"))
