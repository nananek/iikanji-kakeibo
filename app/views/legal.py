"""法的文書 (利用規約 / プライバシーポリシー / 特商法表記) の静的ページ。

運営者情報 (氏名・住所・電話・メール等) は `config.OPERATOR_*` から
注入する。テンプレート本体には運営者依存情報をハードコードしない
(Phase 1 #66 方針)。

URL: `/legal/terms` / `/legal/privacy` / `/legal/tokushoho`
"""

from flask import Blueprint, abort, current_app, render_template


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
