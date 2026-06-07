import json
import re
from datetime import date, datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, make_response, current_app, session as flask_session
from flask_login import login_required, logout_user, current_user

from app.extensions import db, limiter
from app.models.user import User
from app.services.audit import get_effective_user_id
from app.models.account import Account, AccountType
from app.models.webauthn import WebAuthnCredential
from app.models.ai_config import UserAIConfig
from app.models.api_key import APIKey, ALL_SCOPES, SCOPE_LABELS, SCOPE_DEPENDENCIES
from app.models.oauth import OAuthToken
from app.models.audit import AuditGrant, AuditGrantAccount
from app.models.auto_import import AutoImportSource, WebhookConfig
from app.services.ai_receipt import (
    encrypt_api_key, PROVIDER_DEFAULTS, PROVIDER_LABELS,
    get_available_provider_labels, is_llama_cpp_available,
)
from app.views.accounts import TAX_CATEGORIES
from app.models.fiscal import FiscalClose
from app.services.mail import send_email
from app.services.fiscal import (
    PERIOD_LABELS, get_closed_period, close_period, reopen_period, is_year_open,
)
from app.views.helpers import safe_user_error

bp = Blueprint("settings", __name__, url_prefix="/settings")

# HTTP ステータスコード形式のエラーはそのまま返し、
# それ以外の内部例外メッセージは隠蔽する
_HTTP_STATUS_RE = re.compile(r"^HTTP \d{3}$")


def _safe_connection_error(err: str | None) -> str:
    """接続テストのエラーメッセージを安全な形に変換する。"""
    if not err:
        return "不明なエラー"
    if _HTTP_STATUS_RE.match(err):
        return err
    return "サーバーに接続できませんでした。URL・認証情報を確認してください。"


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


@bp.route("/passkeys/<int:credential_id>/delete", methods=["POST"])
@login_required
def delete_passkey(credential_id):
    """Passkey 削除。"""
    credential = WebAuthnCredential.query.filter_by(
        id=credential_id, user_id=current_user.id
    ).first_or_404()

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


# --- TOTP 二段階認証 ---


def _send_totp_security_alert(event_type: str, event_label: str):
    """TOTP 有効化/無効化のセキュリティ通知メール (送信失敗は無視)。"""
    if not current_user.email:
        return
    send_email(
        current_user.email,
        "security_alert",
        {
            "username": current_user.username,
            "event_type": event_type,
            "event_label": event_label,
            "event_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
            "client_ip": request.remote_addr or "不明",
            "user_agent": request.headers.get("User-Agent", "") or "不明",
        },
    )


@bp.route("/totp")
@login_required
def totp_status():
    """TOTP 設定ページ (有効 / 未設定 / 登録途中)。"""
    enrolling = (
        not current_user.totp_enabled
        and current_user.totp_secret_encrypted is not None
    )
    return render_template(
        "settings/totp.html",
        totp_enabled=current_user.totp_enabled,
        enrolling=enrolling,
    )


@bp.route("/totp/begin", methods=["POST"])
@login_required
@limiter.limit("10/hour")
def totp_begin():
    """TOTP 登録開始。secret を生成・暗号化保存し QR を表示 (未有効化のまま)。"""
    from app.services import totp as totp_svc

    if current_user.totp_enabled:
        flash("二段階認証は既に有効です。", "info")
        return redirect(url_for("settings.totp_status"))

    secret = totp_svc.generate_secret()
    current_user.totp_secret_encrypted = totp_svc.encrypt_secret(secret)
    current_user.totp_confirmed_at = None
    current_user.totp_last_used_step = None
    db.session.commit()

    uri = totp_svc.provisioning_uri(secret, current_user.username)
    return render_template(
        "settings/totp_setup.html",
        qr_svg=totp_svc.qr_svg(uri),
        secret=secret,
    )


@bp.route("/totp/confirm", methods=["POST"])
@login_required
@limiter.limit("5/minute")
def totp_confirm():
    """登録途中の secret に対しコードを検証し、TOTP を有効化する。"""
    from app.services import totp as totp_svc

    if current_user.totp_enabled or current_user.totp_secret_encrypted is None:
        flash("二段階認証の登録が開始されていません。", "warning")
        return redirect(url_for("settings.totp_status"))

    code = request.form.get("code", "")
    secret = totp_svc.decrypt_secret(current_user.totp_secret_encrypted)
    if not totp_svc.verify_code(secret, code):
        flash("認証コードが正しくありません。もう一度お試しください。", "danger")
        uri = totp_svc.provisioning_uri(secret, current_user.username)
        return render_template(
            "settings/totp_setup.html",
            qr_svg=totp_svc.qr_svg(uri),
            secret=secret,
        )

    current_user.totp_enabled = True
    current_user.totp_confirmed_at = datetime.now(timezone.utc)
    current_user.totp_last_used_step = totp_svc.current_step()
    db.session.commit()
    current_app.logger.info("totp_enabled: user_id=%s", current_user.id)
    _send_totp_security_alert("totp_enabled", "二段階認証が有効化されました")

    flash("二段階認証を有効にしました。", "success")
    return redirect(url_for("settings.totp_status"))


@bp.route("/totp/cancel", methods=["POST"])
@login_required
@limiter.limit("10/hour")
def totp_cancel():
    """未確定 (有効化前) の登録をキャンセルし secret を破棄する。"""
    if current_user.totp_enabled:
        flash("有効な二段階認証は無効化から行ってください。", "warning")
        return redirect(url_for("settings.totp_status"))
    current_user.totp_secret_encrypted = None
    current_user.totp_confirmed_at = None
    current_user.totp_last_used_step = None
    db.session.commit()
    flash("二段階認証の設定を中止しました。", "info")
    return redirect(url_for("settings.totp_status"))


@bp.route("/totp/disable", methods=["POST"])
@login_required
@limiter.limit("10/hour")
def totp_disable():
    """TOTP を無効化 (パスワード再認証必須)。"""
    if not current_user.totp_enabled:
        return redirect(url_for("settings.totp_status"))

    password = request.form.get("password", "")
    if not password or not current_user.check_password(password):
        flash("パスワードが正しくありません。", "danger")
        return redirect(url_for("settings.totp_status"))

    current_user.totp_enabled = False
    current_user.totp_secret_encrypted = None
    current_user.totp_confirmed_at = None
    current_user.totp_last_used_step = None
    db.session.commit()
    current_app.logger.info("totp_disabled: user_id=%s", current_user.id)
    _send_totp_security_alert("totp_disabled", "二段階認証が無効化されました")

    flash("二段階認証を無効にしました。", "info")
    return redirect(url_for("settings.totp_status"))


# --- AI API 設定 ---


@bp.route("/ai")
@login_required
def ai_config():
    """AI API設定ページ"""
    from app.services.ai_usage import current_month_summary
    config = UserAIConfig.query.filter_by(user_id=current_user.id).first()
    available_labels = get_available_provider_labels()
    # 既存設定の provider が現在利用不可なら警告
    if config and config.provider not in available_labels:
        flash(
            f"現在「{PROVIDER_LABELS.get(config.provider, config.provider)}」は"
            "サーバー側で提供されていません。別のプロバイダーに変更してください。",
            "warning",
        )
    monthly_summary = current_month_summary(current_user.id)
    return render_template(
        "settings/ai_config.html",
        config=config,
        provider_defaults=PROVIDER_DEFAULTS,
        provider_labels=available_labels,
        monthly_summary=monthly_summary,
        provider_display_labels=PROVIDER_LABELS,
    )


@bp.route("/ai/save", methods=["POST"])
@login_required
def ai_config_save():
    """AI API設定の保存"""
    provider = request.form.get("provider", "openai")
    api_key = request.form.get("api_key", "").strip()
    model_name = request.form.get("model_name", "").strip()
    custom_prompt = request.form.get("custom_prompt", "").strip()

    available_labels = get_available_provider_labels()
    if provider not in available_labels:
        flash("無効なプロバイダーです。", "danger")
        return redirect(url_for("settings.ai_config"))

    # llama.cpp は API キー不要、それ以外は新規時に必須
    is_local = provider == "llama_cpp"
    effective_key = api_key or ("_" if is_local else "")

    config = UserAIConfig.query.filter_by(user_id=current_user.id).first()

    if config:
        config.provider = provider
        config.model_name = model_name
        config.custom_prompt = custom_prompt
        if api_key:
            config.api_key_encrypted = encrypt_api_key(api_key)
        elif is_local and not api_key:
            # llama.cpp でキー未入力なら既存キーがなければダミーを保存
            config.api_key_encrypted = encrypt_api_key("_")
    else:
        if not effective_key:
            flash("APIキーを入力してください。", "danger")
            return redirect(url_for("settings.ai_config"))
        config = UserAIConfig(
            user_id=current_user.id,
            provider=provider,
            api_key_encrypted=encrypt_api_key(effective_key),
            model_name=model_name,
            custom_prompt=custom_prompt,
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
    user_id = get_effective_user_id()
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
    )


@bp.route("/fiscal/open-year", methods=["POST"])
@login_required
def fiscal_open_year():
    """古い年度を開設する"""
    year = request.form.get("year", type=int)
    user_id = get_effective_user_id()

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

    err = close_period(get_effective_user_id(), year, period)
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

    err = reopen_period(get_effective_user_id(), year, period)
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


# --- 顧問アクセス管理 ---


PERMISSION_LABELS = {1: "Lv1: 集計結果のみ", 2: "Lv2: 税務科目のみ", 3: "Lv3: 本人同等"}


@bp.route("/audit")
@login_required
def audit():
    """顧問アクセス管理ページ（個人ユーザー専用）"""
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
    """顧問アクセスの付与"""
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
        flash(f"顧問用アカウント「{username}」が見つかりません。", "danger")
        return redirect(url_for("settings.audit"))

    if auditor.id == current_user.id:
        flash("自分自身にはアクセスを付与できません。", "danger")
        return redirect(url_for("settings.audit"))

    # 有償ゲート: 顧問課金 (auditor 自身) または顧問先課金 (owner が
    # 顧問枠を購入) のいずれかを満たす場合に限り AuditGrant を作成可。
    # セルフホストモードでは UnlimitedBillingClient が常に True を返す。
    from app.services.entitlement import has_entitlement
    if not (
        has_entitlement(auditor, "audit_seat")
        or has_entitlement(current_user, "audit_seat")
    ):
        flash(
            "顧問枠を付与するには、顧問本人の有償プラン契約、"
            "または顧問先 (あなた) 側での顧問枠購入が必要です。",
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

    # 顧問宛に招待メールを送る。失敗はログのみ (`send_email` 内で吸収)
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
    """顧問アクセスの取消"""
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
    user_id = get_effective_user_id()

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

    user_id = get_effective_user_id()
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

    user_id = get_effective_user_id()
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


@bp.route("/auto-import")
@login_required
def auto_import():
    """自動取込設定ページ"""
    sources = (
        AutoImportSource.query
        .filter_by(user_id=current_user.id)
        .order_by(AutoImportSource.created_at.desc())
        .all()
    )
    webhooks = (
        WebhookConfig.query
        .filter_by(user_id=current_user.id)
        .order_by(WebhookConfig.created_at.desc())
        .all()
    )
    # config_json をパースして表示用に展開
    for s in sources:
        s._config = json.loads(s.config_json)
    return render_template(
        "settings/auto_import.html",
        sources=sources,
        webhooks=webhooks,
    )


@bp.route("/auto-import/sources/add", methods=["GET", "POST"])
@login_required
def auto_import_source_add():
    """インポート元の追加"""
    if request.method == "POST":
        from app.services.auto_import import encrypt_credentials

        name = request.form.get("name", "").strip()
        provider = request.form.get("provider", "webdav")
        url = request.form.get("url", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        folder_path = request.form.get("folder_path", "/").strip()

        if not all([name, url, username, password]):
            flash("必須項目を入力してください。", "danger")
            return render_template("settings/auto_import_source_form.html")

        if len(name) > 100 or len(url) > 500 or len(username) > 100:
            flash("入力値が長すぎます。", "danger")
            return render_template("settings/auto_import_source_form.html")

        # SSRF 対策: URL バリデーション
        from app.services.sources import validate_external_url
        ok, err = validate_external_url(url)
        if not ok:
            flash(f"URL が不正です: {err}", "danger")
            return render_template("settings/auto_import_source_form.html")

        # 接続テスト
        from app.services.sources.webdav import WebDAVProvider
        test_provider = WebDAVProvider(url, username, password)
        ok, err = test_provider.test_connection()
        if not ok:
            flash(f"接続テストに失敗しました: {err}", "danger")
            return render_template("settings/auto_import_source_form.html")

        config = {
            "url": url,
            "username": username,
            "folder_path": folder_path,
            "file_extensions": ["jpg", "jpeg", "png", "webp"],
        }

        source = AutoImportSource(
            user_id=current_user.id,
            name=name,
            provider=provider,
            config_json=json.dumps(config, ensure_ascii=False),
            credentials_encrypted=encrypt_credentials({"password": password}),
        )
        db.session.add(source)
        db.session.commit()

        flash(f"インポート元「{name}」を追加しました。", "success")
        return redirect(url_for("settings.auto_import"))

    return render_template("settings/auto_import_source_form.html")


@bp.route("/auto-import/sources/test", methods=["POST"])
@login_required
def auto_import_source_test():
    """フォーム入力値で接続テスト（AJAX）"""
    url = request.form.get("url", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if not all([url, username, password]):
        return jsonify({"ok": False, "message": "必須項目を入力してください。"}), 400

    from app.services.sources import validate_external_url
    ok, err = validate_external_url(url)
    if not ok:
        return jsonify({"ok": False, "message": f"URL が不正です: {err}"}), 400

    from app.services.sources.webdav import WebDAVProvider
    provider = WebDAVProvider(url, username, password)
    ok, err = provider.test_connection()
    if ok:
        files = provider.list_files()
        return jsonify({"ok": True, "message": f"接続成功（{len(files)}件のファイルを検出）"})
    return jsonify({"ok": False, "message": f"接続失敗: {_safe_connection_error(err)}"}), 400


@bp.route("/auto-import/sources/<int:source_id>/test", methods=["POST"])
@login_required
def auto_import_source_test_existing(source_id):
    """既存ソースの接続テスト（AJAX）"""
    source = AutoImportSource.query.filter_by(
        id=source_id, user_id=current_user.id
    ).first_or_404()

    from app.services.auto_import import _build_provider
    try:
        provider = _build_provider(source)
    except ValueError as e:
        from flask import current_app
        current_app.logger.exception("_build_provider failed")
        return jsonify({"ok": False, "message": safe_user_error(e)}), 400

    ok, err = provider.test_connection()
    if ok:
        files = provider.list_files()
        return jsonify({"ok": True, "message": f"接続成功（{len(files)}件のファイルを検出）"})
    return jsonify({"ok": False, "message": f"接続失敗: {_safe_connection_error(err)}"}), 400


@bp.route("/auto-import/sources/<int:source_id>/toggle", methods=["POST"])
@login_required
def auto_import_source_toggle(source_id):
    """インポート元の有効/無効切り替え"""
    source = AutoImportSource.query.filter_by(
        id=source_id, user_id=current_user.id
    ).first_or_404()
    source.is_active = not source.is_active
    db.session.commit()
    status = "有効" if source.is_active else "無効"
    flash(f"「{source.name}」を{status}にしました。", "success")
    return redirect(url_for("settings.auto_import"))


@bp.route("/auto-import/sources/<int:source_id>/delete", methods=["POST"])
@login_required
def auto_import_source_delete(source_id):
    """インポート元の削除"""
    source = AutoImportSource.query.filter_by(
        id=source_id, user_id=current_user.id
    ).first_or_404()
    name = source.name
    db.session.delete(source)
    db.session.commit()
    flash(f"インポート元「{name}」を削除しました。", "success")
    return redirect(url_for("settings.auto_import"))


@bp.route("/auto-import/webhooks/add", methods=["GET", "POST"])
@login_required
def auto_import_webhook_add():
    """Webhook 通知の追加"""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        provider = request.form.get("provider", "discord")
        webhook_url = request.form.get("webhook_url", "").strip()
        events = request.form.getlist("events")

        if not all([name, webhook_url]):
            flash("必須項目を入力してください。", "danger")
            return render_template("settings/auto_import_webhook_form.html")

        if len(name) > 100 or len(webhook_url) > 500:
            flash("入力値が長すぎます。", "danger")
            return render_template("settings/auto_import_webhook_form.html")

        # Webhook URL バリデーション。
        # ドメインは限定しない (Discord 互換の自家ホスト Webhook も許可する)。
        # ペイロードは Discord 形式 (notify._send_discord) のまま送るため、
        # Discord 互換エンドポイントであれば動作する。SSRF 対策の
        # validate_external_url (private/loopback/非 http の拒否) は維持する。
        from app.services.sources import validate_external_url
        ok, err = validate_external_url(webhook_url)
        if not ok:
            flash(f"Webhook URL が不正です: {err}", "danger")
            return render_template("settings/auto_import_webhook_form.html")

        if not events:
            events = ["import_success"]

        webhook = WebhookConfig(
            user_id=current_user.id,
            name=name,
            provider=provider,
            webhook_url=webhook_url,
            events_json=json.dumps(events),
        )
        db.session.add(webhook)
        db.session.commit()

        flash(f"通知設定「{name}」を追加しました。", "success")
        return redirect(url_for("settings.auto_import"))

    return render_template("settings/auto_import_webhook_form.html")


@bp.route("/auto-import/webhooks/<int:webhook_id>/delete", methods=["POST"])
@login_required
def auto_import_webhook_delete(webhook_id):
    """Webhook 通知の削除"""
    webhook = WebhookConfig.query.filter_by(
        id=webhook_id, user_id=current_user.id
    ).first_or_404()
    name = webhook.name
    db.session.delete(webhook)
    db.session.commit()
    flash(f"通知設定「{name}」を削除しました。", "success")
    return redirect(url_for("settings.auto_import"))


@bp.route("/delete-account", methods=["GET", "POST"])
@login_required
@limiter.limit("3/hour", methods=["POST"])
def delete_account():
    """退会フロー (Phase 4 公開運用整備)。

    パスワード再認証 + 同意チェックを経て、ユーザーの全データを物理削除
    する (詳細は `app.services.account_deletion` 参照)。

    代理閲覧中 (`acting_as_user_id` セッション設定) は破壊操作を禁止。
    """
    from app.forms.settings import DeleteAccountForm
    from app.services.account_deletion import delete_user_account

    if flask_session.get("acting_as_user_id") is not None:
        flash("代理閲覧中はアカウントを削除できません。", "danger")
        return redirect(url_for("settings.index"))

    form = DeleteAccountForm()
    if form.validate_on_submit():
        # 全ユーザーがパスワードを持つため、退会には必ずパスワード再認証を要求する。
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
