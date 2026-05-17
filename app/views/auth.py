from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db, limiter
from app.models.user import User
from app.forms.auth import LoginForm, RegisterForm
from app.services.seed import seed_accounts_for_user
from app.services.captcha import is_captcha_enabled, get_captcha_response_field, verify_captcha_token
from app.views.helpers import is_safe_internal_path


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


def _safe_next_url(fallback: str) -> str:
    """next パラメータがアプリ内部の相対パスならそれを、そうでなければ fallback を返す。"""
    next_page = request.args.get("next", "")
    if is_safe_internal_path(next_page):
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
            if user.passkey_only_login:
                flash(
                    "このアカウントはパスキー専用モードです。パスキー、またはリカバリコードでログインしてください。",
                    "warning",
                )
                return render_template("auth/login.html", form=form)
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
            if user.passkey_only_login:
                flash(
                    "このアカウントはパスキー専用モードです。パスキー、またはリカバリコードでログインしてください。",
                    "warning",
                )
                return render_template("auth/login_auditor.html", form=form)
            login_user(user, remember=True)
            return redirect(_safe_next_url(url_for("auditor.dashboard")))
        flash("ユーザー名またはパスワードが正しくありません。", "danger")

    return render_template("auth/login_auditor.html", form=form)


@bp.route("/recovery", methods=["GET", "POST"])
@limiter.limit("5/minute", methods=["POST"])
def recovery_login():
    """リカバリコードでログイン。1 回限り使用 + ログイン後は強制復旧フローへ。"""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        if not _check_captcha():
            return render_template("auth/recovery.html")
        username = request.form.get("username", "").strip()
        code = request.form.get("recovery_code", "").strip()

        user = User.query.filter_by(username=username).first()
        # タイミング攻撃対策: ユーザー不在時もダミーの SHA-256 + compare_digest を
        # 実行し、ユーザー存在/不在による応答時間差を抑制する
        if user is None:
            _verify_recovery_code_dummy(code)
            ok = False
        else:
            ok = user.verify_recovery_code(code)
        if ok:
            user.consume_recovery_code()
            db.session.commit()
            login_user(user, remember=False)
            # 強制復旧フロー: パスキー再登録 + リカバリ再生成まで他操作をブロック
            session["pending_recovery_action"] = True
            session["pending_recovery_user_id"] = user.id
            flash(
                "リカバリコードでログインしました。続けてパスキー登録とリカバリコードの再生成を行ってください。",
                "warning",
            )
            return redirect(url_for("settings.passkeys"))
        # 列挙対策: ユーザー存在/コード誤りを区別しない一律メッセージ
        flash("ユーザー名またはリカバリコードが正しくありません。", "danger")

    return render_template("auth/recovery.html")


def _verify_recovery_code_dummy(code):
    """タイミング攻撃対策のダミー検証（ユーザー不在時に呼ぶ）。

    実際の verify_recovery_code() と同じく SHA-256 + compare_digest を実行する
    ことで、ユーザー存在/不在の時間差を抑制する。
    """
    import hashlib
    import secrets as _secrets
    candidate = hashlib.sha256((code or "x").encode()).hexdigest()
    _secrets.compare_digest(candidate, "0" * 64)
    return False


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
