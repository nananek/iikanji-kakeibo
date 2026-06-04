import json
from datetime import date, datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, make_response, current_app, session as flask_session
from flask_login import login_required, logout_user, current_user

from sqlalchemy.orm import joinedload

from app.extensions import db, limiter
from app.models.user import User
from app.models.account import Account, AccountType
from app.models.webauthn import WebAuthnCredential
from app.models.ai_config import UserAIConfig
from app.models.api_key import APIKey, ALL_SCOPES, SCOPE_LABELS, SCOPE_DEPENDENCIES
from app.models.oauth import OAuthToken
from app.models.audit import AuditGrant, AuditGrantAccount
from app.services.ai_receipt import PROVIDER_LABELS
from app.views.accounts import TAX_CATEGORIES
from app.models.fiscal import FiscalClose
from app.services.mail import send_email
from app.services.fiscal import (
    PERIOD_LABELS, get_closed_period, close_period, reopen_period, is_year_open,
)
from app.views.helpers import safe_user_error, maybe_clear_pending_recovery

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/")
@login_required
def index():
    """設定トップページ"""
    from app.services.entitlement import get_entitlement_summary
    from app.services.storage_quota import get_storage_summary
    # Phase 3 の `HttpBillingClient` 実装前は `BILLING_BACKEND=http` 設定で
    # NotImplementedError が伝播する。設定画面が 500 になるのを避けるため
    # ガードしてセクション非表示に倒す。Phase 3 完了後はこの try は除去可。
    try:
        plan_summary = get_entitlement_summary(current_user)
    except NotImplementedError:
        plan_summary = None
    # `get_storage_summary` も内部で `has_entitlement` を呼ぶため、
    # `HttpBillingClient` 未実装時の `NotImplementedError` 経路で 500 に
    # ならないようガードする (Phase 3 実装完了後はこの try は除去可)。
    try:
        storage_summary = get_storage_summary(current_user)
    except NotImplementedError:
        storage_summary = None
    return render_template(
        "settings/index.html",
        plan_summary=plan_summary,
        storage_summary=storage_summary,
    )


@bp.route("/display")
@login_required
def display():
    """表示設定ページ"""
    return render_template(
        "settings/display.html",
        default_period=current_user.get_pref("reports_default_period", "all"),
        ledger_sort=current_user.get_pref("ledger_sort_order", "asc"),
        projection_method=current_user.get_pref("projection_method", "pro_rata"),
    )


@bp.route("/display/save", methods=["POST"])
@login_required
def display_save():
    """表示設定の保存"""
    period = request.form.get("default_period", "all")
    if period not in ("all", "current_month"):
        period = "all"
    sort = request.form.get("ledger_sort", "asc")
    if sort not in ("asc", "desc"):
        sort = "asc"
    projection = request.form.get("projection_method", "pro_rata")
    if projection not in ("pro_rata", "rolling28", "dow28"):
        projection = "pro_rata"
    current_user.set_pref("reports_default_period", period)
    current_user.set_pref("ledger_sort_order", sort)
    current_user.set_pref("projection_method", projection)
    db.session.commit()
    flash("表示設定を保存しました。", "success")
    return redirect(url_for("settings.display"))


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


@bp.route("/backup")
@login_required
def backup():
    """全データバックアップ (Phase v5 BU-1)。

    クライアントが GET /api/v1/backup/export を叩いて暗号文付き JSON を取り、
    本人 MK で復号 → 平文 JSON ファイルとしてダウンロードする。サーバは
    テンプレートを返すだけ。

    監査ユーザーは他人のデータを export できないため対象外。
    """
    if current_user.user_type == "auditor":
        flash("監査アカウントは全データバックアップの対象外です。", "info")
        return redirect(url_for("settings.index"))
    return render_template("settings/backup.html")


@bp.route("/export")
@login_required
def export():
    """全データエクスポート (E6 #113 §15.4 PR-1)。

    クライアントが GET /api/v1/backup/export で暗号文付き JSON を取り、本人 MK
    で復号 → 人間可読 CSV + 証憑画像 + 機械可読 backup.json を fflate で zip 化
    してダウンロードする。サーバはテンプレートを返すだけ (新規 API なし)。

    監査ユーザーは他人のデータを export できないため対象外。
    """
    if current_user.user_type == "auditor":
        flash("監査アカウントは全データエクスポートの対象外です。", "info")
        return redirect(url_for("settings.index"))
    return render_template("settings/export.html")


@bp.route("/restore")
@login_required
def restore():
    """全データリストア preview (Phase v5 BU-4a)。

    ローカルのバックアップファイル (.json or .ikbackup) を選択してブラウザ
    で復号、内容のサマリを表示するだけの read-only ツール。実際の DB 書き
    込みは将来 PR (BU-4b/c) で追加予定。

    監査ユーザーは対象外。
    """
    if current_user.user_type == "auditor":
        flash("監査アカウントは全データリストアの対象外です。", "info")
        return redirect(url_for("settings.index"))
    return render_template("settings/restore.html")


@bp.route("/encryption-keys")
@login_required
def encryption_keys():
    """E2EE 鍵管理ウィザード (v5.0 準備)。

    クライアントサイドで MK 派生・wrap が完結するため、本 view は単にテンプレートを
    返すだけ。実際の wrapped_keys CRUD は `/api/v1/wrapped-keys` (api_v1 blueprint) を
    Alpine.js コンポーネントが叩く。
    """
    # 監査ユーザーは E2EE 機能の対象外 (Lv1/Lv2/Lv3 は本人 MK にアクセスしない)
    if current_user.user_type == "auditor":
        flash("監査アカウントは暗号鍵設定の対象外です。", "info")
        return redirect(url_for("settings.index"))
    return render_template("settings/encryption_keys.html")


@bp.route("/passkeys/<int:credential_id>/delete", methods=["POST"])
@login_required
def delete_passkey(credential_id):
    """Passkey 削除。パスキー専用モードでは最後の 1 本を削除させない。"""
    credential = WebAuthnCredential.query.filter_by(
        id=credential_id, user_id=current_user.id
    ).first_or_404()

    # パスキー専用モード中は、最後の 1 本を削除するとログイン不能になる。
    # 削除を拒否し、リカバリコード再生成 + モード解除を促す。
    if current_user.passkey_only_login:
        remaining = WebAuthnCredential.query.filter_by(
            user_id=current_user.id
        ).count()
        if remaining <= 1:
            block_msg = (
                "パスキー専用モードでは最後のパスキーを削除できません。"
                "先にリカバリコードを再生成し、パスキー専用モードを解除してください。"
            )
            if request.headers.get("HX-Request"):
                resp = make_response("", 422)
                resp.headers["HX-Trigger"] = json.dumps(
                    {"showToast": {"message": block_msg, "type": "danger"}}
                )
                return resp
            flash(block_msg, "danger")
            return redirect(url_for("settings.passkeys"))

    name = credential.name or f"Passkey #{credential.id}"
    db.session.delete(credential)
    db.session.commit()

    if request.headers.get("HX-Request"):
        resp = make_response("", 200)
        resp.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": f"Passkey「{name}」を削除しました。", "type": "success"}}
        )
        return resp

    flash(f"Passkey「{name}」を削除しました。", "success")
    return redirect(url_for("settings.passkeys"))


@bp.route("/passkeys/passkey-only/enable", methods=["POST"])
@login_required
def passkey_only_enable():
    """パスキー専用モードを有効化。

    前提: パスキー >=1 本 + 有効リカバリコード。
    両方揃っていない場合は flash で誘導し、フラグは変更しない。
    """
    passkey_count = WebAuthnCredential.query.filter_by(
        user_id=current_user.id
    ).count()
    if passkey_count < 1:
        flash(
            "パスキー専用モードに切り替える前に、パスキーを 1 つ以上登録してください。",
            "warning",
        )
        return redirect(url_for("settings.passkeys"))
    if not current_user.has_active_recovery_code:
        flash(
            "パスキー専用モードに切り替える前に、有効なリカバリコードを生成してください。",
            "warning",
        )
        return redirect(url_for("settings.passkeys"))

    current_user.passkey_only_login = True
    db.session.commit()
    current_app.logger.info(
        "passkey_only_enabled: user_id=%s", current_user.id
    )
    flash(
        "パスキー専用モードを有効にしました。今後はパスキーまたはリカバリコードでのみログインできます。",
        "success",
    )
    return redirect(url_for("settings.passkeys"))


@bp.route("/passkeys/passkey-only/disable", methods=["POST"])
@login_required
def passkey_only_disable():
    """パスキー専用モードを解除（パスワード再認証必須）。"""
    password = request.form.get("password", "")
    if not password or not current_user.check_password(password):
        flash("パスワードが正しくありません。", "danger")
        return redirect(url_for("settings.passkeys"))

    current_user.passkey_only_login = False
    db.session.commit()
    current_app.logger.info(
        "passkey_only_disabled: user_id=%s", current_user.id
    )
    flash(
        "パスキー専用モードを解除しました。パスワードでのログインも可能になりました。",
        "info",
    )
    return redirect(url_for("settings.passkeys"))


@bp.route("/passkeys/recovery/generate", methods=["POST"])
@login_required
def recovery_generate():
    """非常用リカバリコードを生成（パスワード再認証必須）。

    既存コードがあれば即時無効化（上書き）。生コードを 1 回だけ表示画面に
    レンダリングし、cookie/flash には載せない（漏洩防止）。
    """
    password = request.form.get("password", "")
    if not password or not current_user.check_password(password):
        flash("パスワードが正しくありません。", "danger")
        return redirect(url_for("settings.passkeys"))

    raw = current_user.set_recovery_code()
    db.session.commit()
    current_app.logger.info(
        "recovery_code_generated: user_id=%s prefix=%s",
        current_user.id, current_user.recovery_code_prefix,
    )

    # リカバリログイン後の強制復旧フロー解除判定
    maybe_clear_pending_recovery(current_user, flask_session)

    return render_template(
        "settings/recovery_code_show.html",
        raw_code=raw,
        prefix=current_user.recovery_code_prefix,
    )


# --- AI API 設定 ---


@bp.route("/ai")
@login_required
def ai_config():
    """AI API設定ページ"""
    from app.services.ai_usage import current_month_summary
    config = UserAIConfig.query.filter_by(user_id=current_user.id).first()
    available_labels = PROVIDER_LABELS
    # 既存設定の provider が現在サポート外 (例: v5.0 で廃止した llama_cpp) なら警告。
    # 廃止済 provider は PROVIDER_LABELS に無いため、表示用ラベルを別途補完する。
    _DEPRECATED_PROVIDER_LABELS = {"llama_cpp": "llama.cpp (サーバー提供)"}
    if config and config.provider not in available_labels:
        provider_label = _DEPRECATED_PROVIDER_LABELS.get(
            config.provider, config.provider
        )
        flash(
            f"現在「{provider_label}」はサポートされていません。"
            "別のプロバイダーに変更してください。",
            "warning",
        )
    monthly_summary = current_month_summary(current_user.id)
    return render_template(
        "settings/ai_config.html",
        config=config,
        provider_labels=available_labels,
        monthly_summary=monthly_summary,
        provider_display_labels=PROVIDER_LABELS,
    )


# 旧 form POST /ai/save エンドポイント (Fernet 暗号化保存) は E2EE 化により
# 廃止。AI 設定の保存は ai_config.html の Alpine + JS から PUT /api/v1/ai-config
# (E2EE blob/iv 形式) に統一されている。


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


# --- AI API 利用履歴 ---


def _parse_usage_filters():
    """`/settings/ai-usage` 系のクエリパラメータをパースする。"""
    from datetime import date as _date
    args = request.args
    start_str = (args.get("start") or "").strip()
    end_str = (args.get("end") or "").strip()
    start = end = None
    try:
        if start_str:
            start = _date.fromisoformat(start_str)
    except ValueError:
        start = None
    try:
        if end_str:
            end = _date.fromisoformat(end_str)
    except ValueError:
        end = None
    provider = (args.get("provider") or "").strip() or None
    feature = (args.get("feature") or "").strip() or None
    try:
        page = max(1, int(args.get("page", "1")))
    except ValueError:
        page = 1
    return start, end, provider, feature, page


@bp.route("/ai-usage")
@login_required
def ai_usage():
    """AI API 利用履歴ページ"""
    from app.services.ai_usage import (
        query_logs, monthly_summary, FEATURE_LABELS, STATUS_LABELS,
    )
    start, end, provider, feature, page = _parse_usage_filters()
    items, total, pages, page = query_logs(
        current_user.id, start=start, end=end,
        provider=provider, feature=feature, page=page,
    )
    summary = monthly_summary(
        current_user.id, start=start, end=end,
        provider=provider, feature=feature,
    )
    return render_template(
        "settings/ai_usage.html",
        items=items, total=total, pages=pages, page=page,
        summary=summary,
        filter_start=start.isoformat() if start else "",
        filter_end=end.isoformat() if end else "",
        filter_provider=provider or "",
        filter_feature=feature or "",
        provider_labels=PROVIDER_LABELS,
        feature_labels=FEATURE_LABELS,
        status_labels=STATUS_LABELS,
    )


@bp.route("/ai-usage/export.csv")
@login_required
def ai_usage_export_csv():
    """AI API 利用履歴を CSV (BOM + UTF-8) でエクスポート"""
    import csv
    import io
    from flask import Response
    from app.services.ai_usage import (
        iter_logs_for_export, FEATURE_LABELS, STATUS_LABELS,
    )
    start, end, provider, feature, _page = _parse_usage_filters()
    items = iter_logs_for_export(
        current_user.id, start=start, end=end,
        provider=provider, feature=feature,
    )
    output = io.StringIO()
    output.write("﻿")  # BOM (Excel 文字化け防止)
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "日時 (UTC)", "プロバイダー", "モデル", "機能",
        "入力トークン", "出力トークン", "合計トークン",
        "レイテンシ (ms)", "ステータス", "HTTPステータス",
    ])
    for log in items:
        writer.writerow([
            log.created_at.isoformat(),
            log.provider,
            log.model,
            FEATURE_LABELS.get(log.feature, log.feature),
            log.input_tokens if log.input_tokens is not None else "",
            log.output_tokens if log.output_tokens is not None else "",
            log.total_tokens if log.total_tokens is not None else "",
            log.latency_ms if log.latency_ms is not None else "",
            STATUS_LABELS.get(log.status, log.status),
            log.http_status if log.http_status is not None else "",
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                'attachment; filename="ai-usage.csv"',
        },
    )


@bp.route("/ai-usage/clear", methods=["POST"])
@login_required
def ai_usage_clear():
    """AI API 利用履歴を全削除 (本人のみ)"""
    from app.services.ai_usage import delete_all_for_user
    if request.form.get("confirm") != "DELETE":
        flash("削除確認の入力が一致しません。", "danger")
        return redirect(url_for("settings.ai_usage"))
    deleted = delete_all_for_user(current_user.id)
    current_app.logger.info(
        "ai_usage_logs cleared: user_id=%s deleted=%s",
        current_user.id, deleted,
    )
    flash(f"AI API 利用履歴 {deleted} 件を削除しました。", "success")
    return redirect(url_for("settings.ai_usage"))


# --- API キー ---


@bp.route("/api-keys")
@login_required
def api_keys():
    """APIキー管理ページ"""
    keys = (
        APIKey.query.filter_by(user_id=current_user.id)
        .order_by(APIKey.created_at.desc())
        .all()
    )
    return render_template(
        "settings/api_keys.html",
        keys=keys,
        all_scopes=ALL_SCOPES,
        scope_labels=SCOPE_LABELS,
    )


@bp.route("/api-keys/create", methods=["POST"])
@login_required
def api_key_create():
    """APIキーの発行"""
    name = request.form.get("name", "").strip()
    if not name:
        flash("キーの名前を入力してください。", "danger")
        return redirect(url_for("settings.api_keys"))

    # スコープ収集・依存解決
    selected = set(request.form.getlist("scopes"))
    for scope, dep in SCOPE_DEPENDENCIES.items():
        if scope in selected:
            selected.add(dep)
    scopes = ",".join(s for s in ALL_SCOPES if s in selected)
    if not scopes:
        scopes = "journals:create"

    raw_key, key_hash, key_prefix = APIKey.generate()
    api_key = APIKey(
        user_id=current_user.id,
        name=name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes=scopes,
    )
    db.session.add(api_key)
    db.session.commit()

    # 生キーをフラッシュメッセージで1回だけ表示
    keys = (
        APIKey.query.filter_by(user_id=current_user.id)
        .order_by(APIKey.created_at.desc())
        .all()
    )
    return render_template(
        "settings/api_keys.html",
        keys=keys,
        new_key=raw_key,
        new_key_name=name,
        all_scopes=ALL_SCOPES,
        scope_labels=SCOPE_LABELS,
    )


@bp.route("/api-keys/<int:key_id>/delete", methods=["POST"])
@login_required
def api_key_delete(key_id):
    """APIキーの削除"""
    api_key = APIKey.query.filter_by(
        id=key_id, user_id=current_user.id
    ).first_or_404()
    name = api_key.name
    db.session.delete(api_key)
    db.session.commit()

    if request.headers.get("HX-Request"):
        resp = make_response("", 200)
        resp.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": f"APIキー「{name}」を削除しました。", "type": "success"}}
        )
        return resp

    flash(f"APIキー「{name}」を削除しました。", "success")
    return redirect(url_for("settings.api_keys"))


# --- OAuth トークン管理 ---


@bp.route("/oauth-tokens")
@login_required
def oauth_tokens():
    """OAuth Device Flow で発行したアクセストークンの一覧"""
    tokens = (
        OAuthToken.query.filter_by(user_id=current_user.id, is_active=True)
        .order_by(OAuthToken.created_at.desc())
        .all()
    )
    return render_template("settings/oauth_tokens.html", tokens=tokens)


@bp.route("/oauth-tokens/<int:token_id>/revoke", methods=["POST"])
@login_required
def oauth_token_revoke(token_id):
    """OAuth トークンの取り消し"""
    token = OAuthToken.query.filter_by(
        id=token_id, user_id=current_user.id
    ).first_or_404()
    name = token.name
    token.is_active = False
    token.revoked_at = datetime.now(timezone.utc)
    db.session.commit()

    if request.headers.get("HX-Request"):
        resp = make_response("", 200)
        resp.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": f"トークン「{name}」を取り消しました。", "type": "success"}}
        )
        return resp

    flash(f"トークン「{name}」を取り消しました。", "success")
    return redirect(url_for("settings.oauth_tokens"))


# --- 月次確定 ---


@bp.route("/fiscal")
@login_required
def fiscal():
    """月次確定管理ページ"""
    year = request.args.get("year", date.today().year, type=int)
    user_id = current_user.id
    year_open = is_year_open(user_id, year)
    closed = get_closed_period(user_id, year)

    periods = []
    for p in range(0, 17):
        if p == 16:
            # 損益振替は決算月3確定で自動生成・解除で自動削除
            periods.append({
                "number": p,
                "label": PERIOD_LABELS[p],
                "closed": closed >= 15,
                "can_close": False,
                "can_reopen": False,
                "auto": True,
            })
        else:
            periods.append({
                "number": p,
                "label": PERIOD_LABELS[p],
                "closed": p <= closed,
                "can_close": p == closed + 1 and year_open,
                "can_reopen": p == closed,
            })

    return render_template(
        "settings/fiscal.html",
        year=year,
        periods=periods,
        closed_period=closed,
        year_open=year_open,
        closing_accounts_meta=_closing_accounts_meta(user_id),
    )


def _closing_accounts_meta(user_id):
    """#338 item1: 決算月3 確定時のクライアント closing 生成が科目を分類するための
    code → {type, system_role} メタ。type で収益/費用の振替対象を判定し、
    system_role=retained_earnings で繰越利益科目を引く。account テーブルは
    非暗号化メタなのでサーバで構築してよい。closing は当年度の全科目を対象に
    するため is_active で絞らず全科目を含める。"""
    accounts = (
        Account.query.filter_by(user_id=user_id)
        .options(joinedload(Account.account_type))
        .all()
    )
    return {
        a.code: {
            "type": a.account_type.code,
            "system_role": a.system_role,
        }
        for a in accounts
    }


@bp.route("/fiscal/open-year", methods=["POST"])
@login_required
def fiscal_open_year():
    """古い年度を開設する"""
    year = request.form.get("year", type=int)
    user_id = current_user.id

    if is_year_open(user_id, year):
        flash(f"{year}年度は既に開設済みです。", "info")
        return redirect(url_for("settings.fiscal", year=year))

    # 開設済み最古の年度の期首が確定済みなら、それ以前は開設不可
    earliest_fc = (
        FiscalClose.query
        .filter(FiscalClose.user_id == user_id, FiscalClose.closed_period >= 0)
        .order_by(FiscalClose.year)
        .first()
    )
    if earliest_fc and year < earliest_fc.year and earliest_fc.closed_period >= 0:
        flash(
            f"{earliest_fc.year}年度の期首振戻が確定済みのため、"
            f"{year}年度は開設できません。",
            "danger",
        )
        return redirect(url_for("settings.fiscal", year=year))

    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    if not fc:
        fc = FiscalClose(user_id=user_id, year=year, closed_period=-1)
        db.session.add(fc)
        db.session.commit()

    flash(f"{year}年度を開設しました。", "success")
    return redirect(url_for("settings.fiscal", year=year))


@bp.route("/fiscal/close", methods=["POST"])
@login_required
def fiscal_close():
    """月次確定を実行"""
    is_htmx = bool(request.headers.get("HX-Request"))
    is_ajax = is_htmx or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    year = request.form.get("year", type=int)
    period = request.form.get("period", type=int)

    # #338 item1: 決算月3 (period15) の確定はクライアントが損益振替を暗号化生成して
    # POST /api/v1/fiscal/close-closing 経由で行う (closing_hook.mjs)。サーバは MK を
    # 持たず closing を生成できないため、この htmx 経路では period15 を受け付けない。
    if period == 15:
        msg = "決算月3は決算画面の確定ボタンから確定してください。"
        if is_htmx:
            resp = make_response("", 422)
            resp.headers["HX-Trigger"] = json.dumps(
                {"showToast": {"message": msg, "type": "danger"}}
            )
            return resp
        if is_ajax:
            return jsonify({"ok": False, "message": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("settings.fiscal", year=year))

    err = close_period(current_user.id, year, period)
    label = PERIOD_LABELS.get(period, f"{period}月")

    if is_htmx:
        msg = err or f"{year}年{label}を確定しました。"
        resp = make_response("", 422 if err else 200)
        trigger = {"showToast": {"message": msg, "type": "danger" if err else "success"}}
        if not err:
            resp.headers["HX-Refresh"] = "true"
        resp.headers["HX-Trigger"] = json.dumps(trigger)
        return resp

    if is_ajax:
        if err:
            return jsonify({"ok": False, "message": err}), 400
        return jsonify({"ok": True, "message": f"{year}年{label}を確定しました。"})

    if err:
        flash(err, "danger")
    else:
        flash(f"{year}年{label}を確定しました。", "success")
    return redirect(url_for("settings.fiscal", year=year))


@bp.route("/fiscal/reopen", methods=["POST"])
@login_required
def fiscal_reopen():
    """月次確定を解除"""
    is_htmx = bool(request.headers.get("HX-Request"))
    is_ajax = is_htmx or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    year = request.form.get("year", type=int)
    period = request.form.get("period", type=int)

    err = reopen_period(current_user.id, year, period)
    label = PERIOD_LABELS.get(period, f"{period}月")

    if is_htmx:
        msg = err or f"{year}年{label}の確定を解除しました。"
        resp = make_response("", 422 if err else 200)
        trigger = {"showToast": {"message": msg, "type": "danger" if err else "success"}}
        if not err:
            resp.headers["HX-Refresh"] = "true"
        resp.headers["HX-Trigger"] = json.dumps(trigger)
        return resp

    if is_ajax:
        if err:
            return jsonify({"ok": False, "message": err}), 400
        return jsonify({"ok": True, "message": f"{year}年{label}の確定を解除しました。"})

    if err:
        flash(err, "danger")
    else:
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

    # 有償ゲート: 監査者課金 (auditor 自身) または被監査者課金 (owner が
    # 監査枠を購入) のいずれかを満たす場合に限り AuditGrant を作成可。
    # セルフホストモードでは UnlimitedBillingClient が常に True を返す。
    from app.services.entitlement import has_entitlement
    if not (
        has_entitlement(auditor, "audit_seat")
        or has_entitlement(current_user, "audit_seat")
    ):
        flash(
            "監査枠を付与するには、監査者本人の有償プラン契約、"
            "または被監査者 (あなた) 側での監査枠購入が必要です。",
            "warning",
        )
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
                    account_user_id=account.user_id,
                    account_code=account.code,
                ))

    db.session.commit()

    # 監査者宛に招待メールを送る。失敗はログのみ (`send_email` 内で吸収)
    # で本体フローに影響しない。
    if auditor.email:
        from app.services.mail import send_email
        send_email(
            auditor.email,
            "audit_invitation",
            {
                "auditor_username": auditor.username,
                "owner_username": current_user.username,
                "permission_label": PERMISSION_LABELS[level],
                "login_url": url_for("auth.login_auditor", _external=True),
            },
        )

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

    published_codes = {
        ga.account_code for ga in grant.grant_accounts
    }

    tax_category_labels = {k: v for k, v in TAX_CATEGORIES if k}

    return render_template(
        "settings/audit_accounts.html",
        grant=grant,
        account_types=account_types,
        accounts=accounts,
        published_codes=published_codes,
        permission_labels=PERMISSION_LABELS,
        tax_category_labels=tax_category_labels,
    )


@bp.route("/audit/<int:grant_id>/packages")
@login_required
def audit_packages(grant_id):
    """監査スナップショットの送信ページ (owner 側, §14.5)。

    HPKE seal・スナップショット生成・送信はすべてクライアント (audit/packages_renderer)
    が行う。本ビューは描画に必要なメタ (科目・年度・grant) を JSON island で渡すだけ。
    """
    if current_user.user_type != "personal":
        flash("この機能は個人ユーザー専用です。", "warning")
        return redirect(url_for("dashboard.index"))

    grant = AuditGrant.query.filter_by(
        id=grant_id, owner_user_id=current_user.id
    ).first_or_404()
    if grant.revoked_at is not None:
        flash("この監査アクセスは失効しています。", "warning")
        return redirect(url_for("settings.audit"))

    # 遅延 import: 年度リスト用 (settings の top import を増やさない)。
    from app.models.journal import JournalEntry

    accounts = (
        Account.query
        .filter_by(user_id=current_user.id, is_active=True)
        .order_by(Account.code)
        .all()
    )
    # Lv2 は公開科目 (AuditGrantAccount) のみメタに含め、非公開科目名を監査者に
    # 漏らさない。Lv1 (集計のみ) / Lv3 (本人同等) は全科目。
    if grant.permission_level == 2:
        published = {ga.account_code for ga in grant.grant_accounts}
        accounts = [a for a in accounts if a.code in published]

    accounts_meta = {
        a.code: {
            "type": a.account_type.code,
            "normal_balance": a.account_type.normal_balance,
            "name": a.name,
            "tax_category": a.tax_category,
        }
        for a in accounts
    }

    # fiscal_year は平文カラム。仕訳のある年度を新しい順に提示する。
    year_rows = (
        db.session.query(JournalEntry.fiscal_year)
        .filter(
            JournalEntry.user_id == current_user.id,
            JournalEntry.fiscal_year.isnot(None),
        )
        .distinct()
        .order_by(JournalEntry.fiscal_year.desc())
        .all()
    )
    fiscal_years = [r[0] for r in year_rows]

    return render_template(
        "settings/audit_packages.html",
        grant=grant,
        accounts_meta=accounts_meta,
        fiscal_years=fiscal_years,
        permission_labels=PERMISSION_LABELS,
    )


@bp.route("/audit/<int:grant_id>/accounts", methods=["POST"])
@login_required
def audit_accounts_save(grant_id):
    """公開科目の保存"""
    grant = AuditGrant.query.filter_by(
        id=grant_id, owner_user_id=current_user.id, permission_level=2
    ).first_or_404()

    selected_codes = set(request.form.getlist("account_codes"))

    # 事業主は常に含める
    proprietor = Account.query.filter_by(
        user_id=current_user.id, system_role="proprietor"
    ).first()
    if proprietor:
        selected_codes.add(proprietor.code)

    # 既存をクリアして再作成
    AuditGrantAccount.query.filter_by(audit_grant_id=grant.id).delete()
    for acode in selected_codes:
        db.session.add(AuditGrantAccount(
            audit_grant_id=grant.id,
            account_user_id=current_user.id,
            account_code=acode,
        ))

    db.session.commit()
    flash("公開科目を保存しました。", "success")
    return redirect(url_for("settings.audit_accounts", grant_id=grant_id))


# --- 自動取込 ---


# --- 青色申告決算書 ---


@bp.route("/tax-form")
@login_required
def tax_form():
    """青色申告決算書 科目マッピング設定"""
    from app.services.tax_form import get_mappable_fields, get_user_mappings, get_account_mapping

    form_type = request.args.get("form_type", "general")
    if form_type not in ("general", "real_estate"):
        form_type = "general"
    user_id = current_user.id

    fields = get_mappable_fields(form_type)
    field_mappings = get_user_mappings(user_id, form_type)
    account_to_field = get_account_mapping(user_id, form_type)

    # ユーザーの全科目
    accounts = (
        Account.query
        .filter_by(user_id=user_id, is_active=True)
        .order_by(Account.code)
        .all()
    )

    account_types = AccountType.query.order_by(AccountType.display_order).all()

    # セクション名ラベル
    section_labels = {
        "revenue": "売上（収入）",
        "cost_of_sales": "売上原価",
        "expenses": "経費",
        "income": "所得金額",
        "bs_assets": "資産の部",
        "bs_liabilities": "負債・資本の部",
    }

    return render_template(
        "settings/tax_form.html",
        form_type=form_type,
        fields=fields,
        field_mappings=field_mappings,
        account_to_field=account_to_field,
        accounts=accounts,
        account_types=account_types,
        section_labels=section_labels,
    )


@bp.route("/tax-form/save-mappings", methods=["POST"])
@login_required
def tax_form_save_mappings():
    """マッピングの一括保存"""
    from app.services.tax_form import save_mappings

    user_id = current_user.id
    mapping_data = []

    for key, value in request.form.items():
        if key.startswith("mapping_") and value:
            account_code = key.replace("mapping_", "")
            mapping_data.append({
                "account_code": account_code,
                "field_id": value,
            })

    form_type = request.form.get("form_type", "general")
    if form_type not in ("general", "real_estate"):
        form_type = "general"
    save_mappings(user_id, mapping_data, form_type=form_type)
    db.session.commit()
    flash("決算書マッピングを保存しました。", "success")
    return redirect(url_for("settings.tax_form", form_type=form_type))


@bp.route("/tax-form/bulk-create", methods=["POST"])
@login_required
def tax_form_bulk_create():
    """決算書欄から科目を一括作成"""
    from app.services.tax_form import bulk_create_accounts

    user_id = current_user.id
    field_ids = [int(fid) for fid in request.form.getlist("field_ids") if fid]

    form_type = request.form.get("form_type", "general")

    if not field_ids:
        flash("科目を作成する欄を選択してください。", "warning")
        return redirect(url_for("settings.tax_form", form_type=form_type))

    created, skipped = bulk_create_accounts(user_id, field_ids)
    db.session.commit()

    msg = f"{created}件の科目を作成しました。"
    if skipped:
        msg += f"（{len(skipped)}件はコードが既存のためマッピングのみ設定）"
    flash(msg, "success")
    return redirect(url_for("settings.tax_form", form_type=form_type))



@bp.route("/delete-account", methods=["GET", "POST"])
@login_required
@limiter.limit("3/hour", methods=["POST"])
def delete_account():
    """退会フロー (Phase 4 公開運用整備)。

    パスワード再認証 + 同意チェックを経て、ユーザーの全データを削除する。
    電帳法保管対象の `VoucherAuditLog` は user_id NULL 化で匿名化保持、
    他は物理削除 (詳細は `app.services.account_deletion` 参照)。
    """
    from app.forms.settings import DeleteAccountForm
    from app.services.account_deletion import delete_user_account

    form = DeleteAccountForm()
    if form.validate_on_submit():
        # Passkey 専用ユーザーはパスワードを持たないためセッション認証済を
        # 信頼し、パスワード検証はスキップ (GDPR データ消去権の保証)。
        # 通常ユーザーはパスワード再認証を要求する。
        if not current_user.passkey_only_login:
            if not current_user.check_password(form.password.data or ""):
                flash("パスワードが正しくありません。", "danger")
                return render_template(
                    "settings/delete_account.html", form=form,
                )

        # 削除前にメール送信に必要な情報を取得
        username = current_user.username
        email = current_user.email
        user_id = current_user.id
        deleted_at = datetime.now(timezone.utc)

        try:
            delete_user_account(user_id)
        except Exception:
            current_app.logger.exception(
                "delete_account failed for user_id=%d", user_id,
            )
            flash(
                "アカウント削除に失敗しました。お問い合わせフォームから"
                "ご連絡ください。",
                "danger",
            )
            return redirect(url_for("settings.index"))

        # ログアウト (セッション破棄)
        logout_user()
        flask_session.clear()

        # 退会完了メール (送信失敗は握って redirect は続行)
        try:
            send_email(email, "account_deleted", {
                "username": username,
                "deleted_at": deleted_at.strftime("%Y-%m-%d %H:%M UTC"),
            })
        except Exception:
            current_app.logger.exception(
                "delete_account: send_email failed (to=%s)", email,
            )

        flash(
            "アカウントを削除しました。ご利用ありがとうございました。",
            "info",
        )
        return redirect(url_for("auth.login"))

    return render_template("settings/delete_account.html", form=form)
