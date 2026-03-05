import json
from datetime import date, datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, make_response
from flask_login import login_required, current_user

from app.extensions import db
from app.models.user import User
from app.services.audit import get_effective_user_id
from app.models.account import Account, AccountType
from app.models.webauthn import WebAuthnCredential
from app.models.ai_config import UserAIConfig
from app.models.api_key import APIKey, ALL_SCOPES, SCOPE_LABELS, SCOPE_DEPENDENCIES
from app.models.audit import AuditGrant, AuditGrantAccount
from app.models.auto_import import AutoImportSource, WebhookConfig
from app.services.ai_receipt import (
    encrypt_api_key, PROVIDER_DEFAULTS, PROVIDER_LABELS,
)
from app.views.accounts import TAX_CATEGORIES
from app.models.fiscal import FiscalClose
from app.services.fiscal import (
    PERIOD_LABELS, get_closed_period, close_period, reopen_period, is_year_open,
)

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/")
@login_required
def index():
    """設定トップページ"""
    return render_template("settings/index.html")


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
    """Passkey 削除"""
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
    custom_prompt = request.form.get("custom_prompt", "").strip()
    base_url = request.form.get("base_url", "").strip()
    compliance_check = request.form.get("compliance_check") == "on"

    if provider not in PROVIDER_DEFAULTS:
        flash("無効なプロバイダーです。", "danger")
        return redirect(url_for("settings.ai_config"))

    # Ollama は API キー不要、それ以外は新規時に必須
    is_ollama = provider == "ollama"
    effective_key = api_key or ("_" if is_ollama else "")

    config = UserAIConfig.query.filter_by(user_id=current_user.id).first()

    if config:
        config.provider = provider
        config.model_name = model_name
        config.custom_prompt = custom_prompt
        config.base_url = base_url
        config.compliance_check = compliance_check
        if api_key:
            config.api_key_encrypted = encrypt_api_key(api_key)
        elif is_ollama and not api_key:
            # Ollama でキー未入力なら既存キーがなければダミーを保存
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
            base_url=base_url,
            compliance_check=compliance_check,
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
    return jsonify({"ok": False, "message": f"接続失敗: {err}"}), 400


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
        return jsonify({"ok": False, "message": str(e)}), 400

    ok, err = provider.test_connection()
    if ok:
        files = provider.list_files()
        return jsonify({"ok": True, "message": f"接続成功（{len(files)}件のファイルを検出）"})
    return jsonify({"ok": False, "message": f"接続失敗: {err}"}), 400


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

        # Webhook URL バリデーション
        _WEBHOOK_PREFIXES = {
            "discord": "https://discord.com/api/webhooks/",
        }
        expected = _WEBHOOK_PREFIXES.get(provider)
        if expected and not webhook_url.startswith(expected):
            flash(f"Webhook URL は {expected} で始まる必要があります。", "danger")
            return render_template("settings/auto_import_webhook_form.html")

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
