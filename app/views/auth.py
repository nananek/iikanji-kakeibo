import re
from urllib.parse import urlparse

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, abort, current_app
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db, limiter
from app.models.user import User
from app.forms.auth import LoginForm, RegisterForm
from app.services.seed import seed_accounts_for_user
from app.services.captcha import is_captcha_enabled, get_captcha_response_field, verify_captcha_token
from app.views.helpers import is_safe_internal_path


# 内部相対パス用の厳密な regex sanitizer (CodeQL py/url-redirection を
# 満足させるため、関数越しではなく view 内で直接マッチを評価する)。
# 許容: '/' 始まり、ASCII 英数字 + `_ - . / ~` + query/fragment 用記号。
# プロトコル相対 ('//') / バックスラッシュ ('\\') / scheme / netloc を排除。
_INTERNAL_PATH_RE = re.compile(r"\A/[A-Za-z0-9_\-./~?=&%#]*\Z")


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
    if not current_app.config.get("REGISTRATION_ENABLED", True):
        abort(404)
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
            # 登録フォームで規約同意済 → 現行バージョンを記録
            accepted_terms_version=current_app.config.get(
                "CURRENT_TERMS_VERSION", ""
            ),
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
    if not current_app.config.get("REGISTRATION_ENABLED", True):
        abort(404)
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
            accepted_terms_version=current_app.config.get(
                "CURRENT_TERMS_VERSION", ""
            ),
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


@bp.route("/accept-terms", methods=["GET", "POST"])
@login_required
@limiter.limit("10/minute", methods=["POST"])
def accept_terms():
    """既存ユーザーが改訂後の規約に再同意するエンドポイント。

    Phase 1 #66 の再同意フロー。before_request フックで
    `accepted_terms_version != CURRENT_TERMS_VERSION` のユーザーがここに
    リダイレクトされる。POST で同意確認 → 現行バージョンを記録。
    """
    current_version = current_app.config.get("CURRENT_TERMS_VERSION", "")
    # 既に同意済のユーザーが直接 GET した場合はダッシュボードへ。
    # 直接 GET なので `?next=` 引き継ぎは不要 (`_safe_next_url` も不要)。
    if (
        current_version
        and current_user.accepted_terms_version == current_version
    ):
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        if request.form.get("accept_terms"):
            current_user.accepted_terms_version = current_version
            db.session.commit()
            flash("規約への同意を更新しました。", "success")
            # `?next=` で内部相対パスが渡っている場合のみ尊重。それ以外
            # (外部 URL / プロトコル相対 / 不正値) はダッシュボードへ。
            # CodeQL py/url-redirection 対策として、関数越しではなく view 内で
            # 多段サニタイズを直接評価する: (1) regex で許容文字を制限、
            # (2) urlparse で scheme / netloc を再確認、(3) プロトコル相対
            # ('//' / '/\\') を排除、(4) 既存の is_safe_internal_path で
            # 仕上げ。data flow が view 内で完結するので静的解析でも
            # sanitizer として認識される。
            next_candidate = request.args.get("next", "")
            parsed = urlparse(next_candidate)
            if (
                next_candidate
                and _INTERNAL_PATH_RE.fullmatch(next_candidate) is not None
                and not next_candidate.startswith("//")
                and not next_candidate.startswith("/\\")
                and not parsed.scheme
                and not parsed.netloc
                and is_safe_internal_path(next_candidate)
            ):
                return redirect(next_candidate)
            return redirect(url_for("dashboard.index"))
        flash("利用規約・プライバシーポリシーへの同意が必要です。", "danger")
    return render_template("auth/accept_terms.html", current_version=current_version)
