"""法的文書 (利用規約 / プライバシーポリシー / 特商法表記) と
お問い合わせ窓口 (Phase 4 公開運用整備)。

運営者情報 (氏名・住所・電話・メール等) は `config.OPERATOR_*` から
注入する。テンプレート本体には運営者依存情報をハードコードしない
(Phase 1 #66 方針)。

URL:
- `/legal/terms` / `/legal/privacy` / `/legal/tokushoho`
- `/legal/contact` (GET: フォーム / POST: 送信)
"""

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template, request,
    url_for,
)

from app.extensions import limiter
from app.forms.contact import ContactForm
from app.services.captcha import (
    get_captcha_response_field, is_captcha_enabled, verify_captcha_token,
)
from app.services.mail import send_email


bp = Blueprint("legal", __name__, url_prefix="/legal")


# slug → (テンプレートファイル名のベース, 表示タイトル)
PAGES = {
    "terms": ("terms", "利用規約"),
    "privacy": ("privacy", "プライバシーポリシー"),
    "tokushoho": ("tokushoho", "特定商取引法に基づく表記"),
}


def _operator_context() -> dict:
    """環境変数から運営者情報をまとめて取得する。未設定の項目は空文字。"""
    cfg = current_app.config
    return {
        "name": cfg.get("OPERATOR_NAME", "") or "(未設定)",
        "business_form": cfg.get("OPERATOR_BUSINESS_FORM", "") or "(未設定)",
        "address": cfg.get("OPERATOR_ADDRESS", "") or "(未設定)",
        "phone": cfg.get("OPERATOR_PHONE", "") or "(未設定)",
        "email": cfg.get("OPERATOR_EMAIL", "") or "(未設定)",
        "service_name": cfg.get("MAIL_FROM_NAME", "本サービス"),
        "legal_updated_at": cfg.get("OPERATOR_LEGAL_UPDATED_AT", "")
            or "(未設定)",
    }


@bp.route("/<slug>")
def show(slug: str):
    """slug に応じた法的文書ページを返す。未定義 slug は 404。"""
    if slug not in PAGES:
        abort(404)
    template_name, page_title = PAGES[slug]
    return render_template(
        f"legal/{template_name}.html",
        page_title=page_title,
        operator=_operator_context(),
    )


def _check_captcha() -> bool:
    """CAPTCHA 検証。未設定環境では常に True (auth.py と同パターン)。"""
    if not is_captcha_enabled():
        return True
    field = get_captcha_response_field()
    token = request.form.get(field, "")
    if not token or not verify_captcha_token(token):
        flash("CAPTCHA 認証に失敗しました。もう一度お試しください。", "danger")
        return False
    return True


@bp.route("/contact", methods=["GET", "POST"])
@limiter.limit("5/hour", methods=["POST"])
def contact():
    """お問い合わせフォーム。

    送信時の処理:
    1. 運営者宛 (`MAIL_CONTACT_TO`) に内容を転送 (`contact_received_admin`)
    2. 送信者宛に自動返信 (`contact_received`、内容をエコー)

    レート制限は IP/分単位の Flask-Limiter デフォルトキーで 5/hour に
    設定 (お問い合わせ濫用防止)。CAPTCHA 設定がある環境では追加で
    検証される。

    `MAIL_CONTACT_TO` が空の場合は運営者宛通知をスキップし、自動返信
    のみ実行 (= 開発・セルフホスト運用)。本番では運用者が必ず設定する。
    """
    form = ContactForm()
    if form.validate_on_submit():
        if not _check_captcha():
            return render_template(
                "legal/contact.html", form=form,
                page_title="お問い合わせ",
                operator=_operator_context(),
            )

        name = form.name.data.strip()
        email = form.email.data.strip()
        subject_line = (form.subject_line.data or "").strip()
        message = form.message.data.strip()

        context = {
            "name": name,
            "email": email,
            "subject_line": subject_line,
            "message": message,
        }

        # 1. 運営者宛通知 (MAIL_CONTACT_TO が空ならスキップ)
        contact_to = current_app.config.get("MAIL_CONTACT_TO", "") or ""
        if contact_to:
            try:
                send_email(contact_to, "contact_received_admin", context)
            except Exception:
                current_app.logger.exception(
                    "contact: admin notification failed (to=%s)", contact_to,
                )

        # 2. 送信者宛の自動返信
        try:
            send_email(email, "contact_received", context)
        except Exception:
            current_app.logger.exception(
                "contact: auto-reply failed (to=%s)", email,
            )

        flash(
            "お問い合わせを受け付けました。確認用メールを送信しましたので、"
            "ご確認ください。",
            "success",
        )
        return redirect(url_for("legal.contact"))

    return render_template(
        "legal/contact.html", form=form,
        page_title="お問い合わせ",
        operator=_operator_context(),
    )
