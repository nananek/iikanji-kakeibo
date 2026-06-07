import re
from urllib.parse import urlparse

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, abort, current_app
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db, limiter
from app.models.user import User
from app.forms.auth import LoginForm, RegisterForm
from app.services.seed import seed_accounts_for_user
from app.services.captcha import check_captcha_or_flash
from app.views.helpers import is_safe_internal_path


# 内部相対パス用の厳密な regex sanitizer (CodeQL py/url-redirection を
# 満足させるため、関数越しではなく view 内で直接マッチを評価する)。
# 許容: '/' 始まり、ASCII 英数字 + `_ - . / ~` + query/fragment 用記号。
# プロトコル相対 ('//') / バックスラッシュ ('\\') / scheme / netloc を排除。
_INTERNAL_PATH_RE = re.compile(r"\A/[A-Za-z0-9_\-./~?=&%#]*\Z")




def _safe_next_url(fallback: str) -> str:
    """next パラメータがアプリ内部の相対パスならそれを、そうでなければ fallback を返す。"""
    next_page = request.args.get("next", "")
    if is_safe_internal_path(next_page):
        return next_page
    return fallback

bp = Blueprint("auth", __name__)


@bp.route("/auth/recovery-reset", methods=["GET"])
def recovery_reset():
    """リカバリシードによるパスワードリセットの公開ページ (#385 PR-4b-3、設計書 §3.4.1)。

    実際の処理はクライアント JS (recovery_reset.mjs) が /auth/recovery/begin|finish へ
    fetch して行う (サーバに平文シード/パスワードは送らない)。login 派生 MK 未設定環境では
    リセット機構が無効なので 404。
    """
    if not current_app.config.get("LOGIN_SERVER_SECRET"):
        abort(404)
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    return render_template("auth/recovery_reset.html")


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    if form.validate_on_submit():
        if not check_captcha_or_flash():
            return render_template("auth/login.html", form=form)
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.user_type == "personal" and user.check_password(form.password.data):
            # #385 PR-T4: パスキー専用モード廃止。パスワードを持つユーザーは常にパスワード
            # ログイン可 (2FA は別途 TOTP / Passkey)。パスワード未設定ユーザーは check_password
            # が False なのでここに到達しない (Passkey / リカバリで入る)。
            if not user.is_active:
                # §16.5 鍵未設定ロック (E7 #114): 通常の login_user は
                # is_active=False を拒否する。force=True で限定セッションを
                # 張り、migration_lock_gate がロック解決ページ (鍵設定 or
                # 退会) 以外をブロックする。鍵設定が完了すると gate が自己
                # 回復して is_active=True に戻る。ロック中は長期 Cookie が不要
                # なので remember=False。
                login_user(user, remember=False, force=True)
                return redirect(url_for("migration_lock.locked"))
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
        if not check_captcha_or_flash():
            return render_template("auth/login_auditor.html", form=form)
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.user_type == "auditor" and user.check_password(form.password.data):
            # #385 PR-T4: パスキー専用モード廃止 (上記 login と同方針)。
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
        if not check_captcha_or_flash():
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
            if not user.is_active:
                # §16.5 鍵未設定ロック (E7 #114 PR-4b): ロック中は force=True で
                # 限定セッションを張りロック解決ページへ。pending_recovery は
                # 設定しない (pending_recovery_gate と migration_lock_gate が
                # 相互リダイレクトでループするため)。鍵設定で解除後、パスキー/
                # リカバリの再設定は設定画面から行う。
                login_user(user, remember=False, force=True)
                flash(
                    "リカバリコードでログインしました。続けて暗号鍵を設定してロックを解除してください。",
                    "warning",
                )
                return redirect(url_for("migration_lock.locked"))
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
        if not check_captcha_or_flash():
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
        if not check_captcha_or_flash():
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
                # CodeQL py/url-redirection 対策:
                # next_candidate を Flask の url_map で endpoint+args に解決し、
                # url_for() で再構築する。url_for() の戻り値は静的解析が
                # sanitizer として認識するため、user 入力からの taint flow を
                # 完全に切断できる。値そのものは next_candidate と等価。
                try:
                    adapter = current_app.url_map.bind("")
                    endpoint, view_args = adapter.match(
                        parsed.path or "/", method="GET"
                    )
                    return redirect(url_for(endpoint, **view_args))
                except Exception:
                    # 解決失敗 (存在しないパス等) はダッシュボードへフォールバック
                    return redirect(url_for("dashboard.index"))
            return redirect(url_for("dashboard.index"))
        flash("利用規約・プライバシーポリシーへの同意が必要です。", "danger")
    return render_template("auth/accept_terms.html", current_version=current_version)
