import re
import time
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


# TOTP 2FA ゲートの pending セッション有効期限 (秒)
_PENDING_2FA_TTL = 300


def _clear_pending_2fa():
    for k in (
        "pending_2fa_user_id", "pending_2fa_kind",
        "pending_2fa_next", "pending_2fa_ts",
    ):
        session.pop(k, None)


def _login_or_2fa(user, *, kind: str, default_next: str):
    """TOTP 未有効なら即ログイン、有効なら pending-2FA セッションを張って
    /login/totp へ誘導する。

    next URL はこの時点で検証済みの内部相対パスのみを保存する
    (生のユーザー入力はセッションに載せない)。
    """
    next_url = _safe_next_url(default_next)
    if user.totp_enabled:
        session["pending_2fa_user_id"] = user.id
        session["pending_2fa_kind"] = kind
        session["pending_2fa_next"] = next_url
        session["pending_2fa_ts"] = time.time()
        return redirect(url_for("auth.login_totp"))
    login_user(user, remember=True)
    return redirect(next_url)

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
            return _login_or_2fa(
                user, kind="personal", default_next=url_for("dashboard.index")
            )
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
            return _login_or_2fa(
                user, kind="auditor", default_next=url_for("auditor.dashboard")
            )
        flash("ユーザー名またはパスワードが正しくありません。", "danger")

    return render_template("auth/login_auditor.html", form=form)


@bp.route("/login/totp", methods=["GET", "POST"])
@limiter.limit("5/minute", methods=["POST"])
def login_totp():
    """パスワード認証通過後の TOTP 2FA ゲート。

    `login()` / `login_auditor()` が pending-2fa セッションを張ってここへ
    誘導する。コード検証 (リプレイ防止付き) に成功して初めて login_user する。
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    user_id = session.get("pending_2fa_user_id")
    ts = session.get("pending_2fa_ts", 0)
    if not user_id or (time.time() - ts) > _PENDING_2FA_TTL:
        _clear_pending_2fa()
        flash("ログインの有効期限が切れました。もう一度ログインしてください。", "warning")
        return redirect(url_for("auth.login"))

    user = db.session.get(User, user_id)
    # 防御的: TOTP が無効化された等で前提が崩れていればログインへ戻す
    if user is None or not user.totp_enabled or not user.totp_secret_encrypted:
        _clear_pending_2fa()
        return redirect(url_for("auth.login"))

    kind = session.get("pending_2fa_kind", "personal")
    fallback = (
        url_for("auditor.dashboard") if kind == "auditor"
        else url_for("dashboard.index")
    )

    if request.method == "POST":
        from app.services import totp as totp_svc

        code = request.form.get("code", "")
        try:
            secret = totp_svc.decrypt_secret(user.totp_secret_encrypted)
        except ValueError:
            flash(
                "二段階認証の設定に問題があります。管理者にお問い合わせください。",
                "danger",
            )
            return render_template("auth/login_totp.html")

        ok, step = totp_svc.verify_code_with_step(
            secret, code, user.totp_last_used_step
        )
        if ok:
            user.totp_last_used_step = step
            db.session.commit()
            next_url = session.get("pending_2fa_next") or fallback
            if not is_safe_internal_path(next_url):
                next_url = fallback
            _clear_pending_2fa()
            login_user(user, remember=True)
            return redirect(next_url)
        # リプレイと誤コードは区別せず一律メッセージ
        flash("認証コードが正しくありません。", "danger")

    return render_template("auth/login_totp.html")


def _check_invitation_token(form, *, expected_user_type: str):
    """招待モード時にトークンを検証する。返り値 (ok, invitation_or_None)。

    `REGISTRATION_INVITE_ONLY=False` のときは常に (True, None)。
    招待モードで:
    - トークン未指定 / 期限切れ / 使用済 → (False, None)
    - フォーム email と招待 email が不一致 → (False, None)
    - 招待 user_type が期待と異なる (personal/auditor 間違い) → (False, None)
    - OK → (True, InvitationToken)
    """
    if not current_app.config.get("REGISTRATION_INVITE_ONLY", False):
        return True, None
    from app.models.invitation import InvitationToken
    raw = (request.args.get("token") or request.form.get("token") or "").strip()
    invitation = InvitationToken.find_valid(raw)
    if invitation is None:
        flash(
            "招待トークンが無効または期限切れです。"
            "招待メールから再度アクセスしてください。",
            "danger",
        )
        return False, None
    if invitation.user_type != expected_user_type:
        flash(
            "この招待トークンはこの種類のアカウント登録には使えません。",
            "danger",
        )
        return False, None
    submitted_email = (form.email.data or "").strip().lower()
    if invitation.email.lower() != submitted_email:
        flash(
            "招待されたメールアドレスと入力内容が一致しません。",
            "danger",
        )
        return False, None
    return True, invitation


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5/minute", methods=["POST"])
def register():
    if not current_app.config.get("REGISTRATION_ENABLED", True):
        abort(404)
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    # 招待制モードで token クエリパラメータがない GET アクセスは 404
    # (トップページから無差別アクセスされないように)
    invite_only = current_app.config.get("REGISTRATION_INVITE_ONLY", False)
    if (
        invite_only
        and request.method == "GET"
        and not (request.args.get("token") or "").strip()
    ):
        abort(404)

    form = RegisterForm()
    if form.validate_on_submit():
        if not _check_captcha():
            return render_template("auth/register.html", form=form)
        ok, invitation = _check_invitation_token(
            form, expected_user_type="personal",
        )
        if not ok:
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
        # user 作成と invitation.mark_used を単一トランザクションで確定。
        # 旧実装は 2 段 commit のため、間に挟まる seed_accounts_for_user が
        # 例外を投げると user は作成済なのに token 未使用のままになる
        # アトミック性バグがあった (PR #99 review 指摘)。
        db.session.flush()  # user.id を確定 (commit はまだ)
        if invitation is not None:
            invitation.mark_used(user.id)
        db.session.commit()

        # 科目 seed は commit 後の独立処理 (失敗してもユーザー作成は確定)
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

    invite_only = current_app.config.get("REGISTRATION_INVITE_ONLY", False)
    if (
        invite_only
        and request.method == "GET"
        and not (request.args.get("token") or "").strip()
    ):
        abort(404)

    form = RegisterForm()
    if form.validate_on_submit():
        if not _check_captcha():
            return render_template("auth/register_auditor.html", form=form)
        ok, invitation = _check_invitation_token(
            form, expected_user_type="auditor",
        )
        if not ok:
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
        # アトミック性: register() と同じく user + mark_used を単一 tx で
        # 確定する (PR #99 review 指摘)。
        db.session.flush()
        if invitation is not None:
            invitation.mark_used(user.id)
        db.session.commit()

        flash("顧問用アカウントを作成しました。ログインしてください。", "success")
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
            # 仕上げ。
            next_candidate = request.args.get("next", "")
            parsed = urlparse(next_candidate)
            match = _INTERNAL_PATH_RE.fullmatch(next_candidate)
            if (
                next_candidate
                and match is not None
                and not next_candidate.startswith("//")
                and not next_candidate.startswith("/\\")
                and not parsed.scheme
                and not parsed.netloc
                and is_safe_internal_path(next_candidate)
            ):
                # 静的解析が sanitizer として認識するよう、user 入力ではなく
                # `re.fullmatch().group(0)` の戻り値を redirect に渡す。
                # 値そのものは `next_candidate` と同一だが、data flow が
                # `re.Match` 経由で迂回するため py/url-redirection が成立。
                return redirect(match.group(0))
            return redirect(url_for("dashboard.index"))
        flash("利用規約・プライバシーポリシーへの同意が必要です。", "danger")
    return render_template("auth/accept_terms.html", current_version=current_version)
