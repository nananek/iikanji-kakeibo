"""CSV明細取り込みビュー"""

import json

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, jsonify
from flask_login import login_required, current_user

from app.extensions import db, limiter
from app.models.account import Account, AccountType
from app.models.ai_config import UserAIConfig
from app.models.journal import JournalEntry
from app.services.audit import get_effective_user_id
from app.services.csv_import import (
    parse_csv_preview,
    parse_csv_full,
    DATE_FORMATS,
    load_column_profile,
    save_column_profile,
)
from app.services.fiscal import (
    check_period_open_for_new, get_restricted_before_year,
    get_capital_account_code, get_closed_periods_for_dates,
    check_entry_modifiable,
)
from app.views.helpers import (
    get_grouped_accounts, save_import_data, load_import_data, delete_import_data,
    safe_user_error,
)

bp = Blueprint("csv_import", __name__, url_prefix="/csv-import")

MAX_CSV_SIZE = 5 * 1024 * 1024  # 5MB


@bp.route("/", methods=["GET", "POST"])
@login_required
def upload():
    """Step 1: CSVアップロード"""
    grouped_accounts = get_grouped_accounts(get_effective_user_id())

    if request.method == "POST":
        csv_file = request.files.get("csv_file")
        if not csv_file or not csv_file.filename:
            flash("CSVファイルを選択してください。", "danger")
            return render_template(
                "csv_import/upload.html", grouped_accounts=grouped_accounts
            )

        raw_bytes = csv_file.read()
        if len(raw_bytes) > MAX_CSV_SIZE:
            flash("ファイルサイズが大きすぎます（上限5MB）。", "danger")
            return render_template(
                "csv_import/upload.html", grouped_accounts=grouped_accounts
            )

        payment_account_code = request.form.get("payment_account_code")
        if not payment_account_code:
            flash("取込先の口座を選択してください。", "danger")
            return render_template(
                "csv_import/upload.html", grouped_accounts=grouped_accounts
            )

        preview = parse_csv_preview(raw_bytes)
        if not preview["headers"] or not preview["rows"]:
            flash("CSVファイルの内容を読み取れませんでした。", "danger")
            return render_template(
                "csv_import/upload.html", grouped_accounts=grouped_accounts
            )

        # 一時ファイルにCSVデータを保存（Cookieサイズ制限を回避）
        import base64
        key = save_import_data({
            "raw_b64": base64.b64encode(raw_bytes).decode("ascii"),
        })
        session["csv_data_key"] = key
        session["csv_payment_account_code"] = payment_account_code

        return redirect(url_for("csv_import.mapping"))

    return render_template(
        "csv_import/upload.html", grouped_accounts=grouped_accounts
    )


@bp.route("/mapping", methods=["GET", "POST"])
@login_required
def mapping():
    """Step 2: 列マッピング + プレビュー"""
    import base64

    data_key = session.get("csv_data_key")
    payment_account_code = session.get("csv_payment_account_code")
    stored = load_import_data(data_key)
    if not stored or not payment_account_code:
        flash("CSVデータがありません。もう一度アップロードしてください。", "warning")
        return redirect(url_for("csv_import.upload"))

    raw_bytes = base64.b64decode(stored["raw_b64"])
    preview = parse_csv_preview(raw_bytes)
    headers = preview["headers"]
    col_indices = list(range(len(headers)))
    num_cols = len(headers)

    user_id = get_effective_user_id()
    payment_account = Account.query.filter_by(
        user_id=user_id, code=payment_account_code
    ).first()

    # プロファイル読込 / AI自動検出
    saved_mapping = None
    mapping_source = None  # "saved" | "ai" | None

    profile = load_column_profile(user_id, payment_account_code)
    if profile:
        # 列インデックスがCSV列数の範囲内かチェック
        cols_valid = all(
            profile.get(k) is None or 0 <= profile[k] < num_cols
            for k in ("date_col", "desc_col", "deposit_col",
                       "withdrawal_col", "amount_col")
        )
        if cols_valid:
            saved_mapping = profile
            mapping_source = "saved"

    # AI 列推定 UI を出すかは E2EE 形式の AI 設定があるかで判定。
    # llama_cpp はサーバ管理者向けで client-side LLM 呼出に対応していないため
    # ボタン自体を出さない (orchestrator のエラーを生で見せないため)。
    _cfg = UserAIConfig.query.filter_by(user_id=user_id).first()
    _client_side_providers = {"openai", "anthropic", "google"}
    has_ai_config = bool(
        _cfg and _cfg.is_e2ee and _cfg.provider in _client_side_providers
    )

    if request.method == "POST":
        date_col = request.form.get("date_col", type=int)
        desc_col = request.form.get("desc_col", type=int)
        date_format = request.form.get("date_format", "")
        deposit_col = request.form.get("deposit_col", type=int)
        withdrawal_col = request.form.get("withdrawal_col", type=int)

        mapping_data = {
            "date_col": date_col,
            "desc_col": desc_col,
            "deposit_col": deposit_col,
            "withdrawal_col": withdrawal_col,
        }

        if date_col is None or desc_col is None:
            flash("日付列と摘要列は必須です。", "danger")
            return render_template(
                "csv_import/mapping.html",
                headers=headers,
                col_indices=col_indices,
                preview_rows=preview["rows"],
                total_rows=preview["total_rows"],
                date_formats=DATE_FORMATS,
                payment_account=payment_account,
                saved_mapping=saved_mapping,
                mapping_source=mapping_source,
            )

        # フルパース
        parsed = parse_csv_full(raw_bytes, mapping_data, date_format)

        if not parsed:
            flash("有効なデータ行が見つかりませんでした。マッピングを確認してください。", "danger")
            return render_template(
                "csv_import/mapping.html",
                headers=headers,
                col_indices=col_indices,
                preview_rows=preview["rows"],
                total_rows=preview["total_rows"],
                date_formats=DATE_FORMATS,
                payment_account=payment_account,
                saved_mapping=saved_mapping,
                mapping_source=mapping_source,
            )

        # プロファイル保存
        save_column_profile(
            user_id, payment_account_code, mapping_data, date_format,
        )

        # パース結果を一時ファイルに保存してconfirmへ
        serializable = []
        for p in parsed:
            serializable.append({
                "row_num": p["row_num"],
                "date": p["date"].isoformat() if p["date"] else None,
                "description": p["description"],
                "deposit": p["deposit"],
                "withdrawal": p["withdrawal"],
            })
        delete_import_data(data_key)
        parsed_key = save_import_data(serializable)
        session["csv_data_key"] = parsed_key

        return redirect(url_for("csv_import.confirm"))

    return render_template(
        "csv_import/mapping.html",
        headers=headers,
        col_indices=col_indices,
        preview_rows=preview["rows"],
        total_rows=preview["total_rows"],
        date_formats=DATE_FORMATS,
        payment_account=payment_account,
        saved_mapping=saved_mapping,
        mapping_source=mapping_source,
        has_ai_config=has_ai_config,
    )


@bp.route("/api/columns-detect-context", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def columns_detect_context():
    """CSV 列推定 AI のクライアント完結用エンドポイント。

    クライアントが headers + sample_rows を POST し、サーバは prompt 構築用
    の placeholder テンプレ + default_model_by_provider を返す。LLM 呼出は
    クライアント側 csv_columns_detect_orchestrator.js が実行する。
    """
    from app.services.ai_receipt import PROVIDER_DEFAULTS
    from app.services.csv_import import CSV_COLUMN_DETECT_PROMPT_TEMPLATE

    MAX_HEADERS = 50
    MAX_HEADER_LEN = 200
    MAX_CELL_LEN = 1000

    payload = request.get_json(silent=True) or {}
    headers = payload.get("headers")
    sample_rows = payload.get("sample_rows", [])
    if not isinstance(headers, list) or not headers:
        return jsonify({"error": "headers must be a non-empty list"}), 400
    if len(headers) > MAX_HEADERS:
        return jsonify({"error": f"headers exceeds maximum ({MAX_HEADERS})"}), 400
    if any(not isinstance(h, str) or len(h) > MAX_HEADER_LEN for h in headers):
        return jsonify({"error": "each header must be a string under 200 chars"}), 400
    if not isinstance(sample_rows, list):
        return jsonify({"error": "sample_rows must be a list"}), 400

    headers_text = ", ".join(f"[{i}] {h}" for i, h in enumerate(headers))
    sample_lines = []
    for row in sample_rows[:5]:
        if isinstance(row, list):
            # 行のセル数も MAX_HEADERS でキャップ
            # (LLM に渡すのは headers と対応する列だけで十分)
            sample_lines.append(
                ", ".join(str(c)[:MAX_CELL_LEN] for c in row[:MAX_HEADERS]),
            )
    sample_text = "\n".join(sample_lines)

    user_id = get_effective_user_id()
    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    custom_prompt = config.custom_prompt if config else ""

    return jsonify({
        "ok": True,
        "prompt_template": CSV_COLUMN_DETECT_PROMPT_TEMPLATE,
        "headers_text": headers_text,
        "sample_text": sample_text,
        "sample_count": len(sample_lines),
        "num_cols": len(headers),
        "custom_prompt": custom_prompt,
        "default_model_by_provider": {
            k: v for k, v in PROVIDER_DEFAULTS.items() if k != "llama_cpp"
        },
    })


@bp.route("/confirm", methods=["GET"])
@login_required
def confirm():
    """Step 3: 確認画面 (取込は batch API 経由)。

    Phase E3-F-5 で旧サーバ POST 経路を撤去。確定は `submitImportBatch`
    (entries_builder + /api/v1/journals/batch) のみ。E3-F-1 で
    resolve_bearer_or_session が acting_as_user_id を解決するため、監査
    代理閲覧時も batch API で正しいオーナー仕訳として処理される。
    """
    data_key = session.get("csv_data_key")
    payment_account_code = session.get("csv_payment_account_code")
    parsed = load_import_data(data_key)
    if not parsed or not payment_account_code:
        flash("データがありません。もう一度アップロードしてください。", "warning")
        return redirect(url_for("csv_import.upload"))
    user_id = get_effective_user_id()
    payment_account = Account.query.filter_by(user_id=user_id, code=payment_account_code).first()

    expense_type = AccountType.query.filter_by(code="expense").first()
    default_expense = (
        Account.query
        .filter_by(user_id=user_id, account_type_id=expense_type.id, is_active=True)
        .order_by(Account.code)
        .first()
    )
    revenue_type = AccountType.query.filter_by(code="revenue").first()
    default_income = (
        Account.query
        .filter_by(user_id=user_id, account_type_id=revenue_type.id, is_active=True)
        .order_by(Account.code)
        .first()
    )

    restricted_before = get_restricted_before_year(user_id)
    capital_code = get_capital_account_code(user_id)
    closed_periods = get_closed_periods_for_dates(
        user_id, [r.get("date", "") for r in parsed]
    )
    grouped_accounts = get_grouped_accounts(user_id)
    has_ai_config = UserAIConfig.query.filter_by(user_id=user_id).first() is not None
    return render_template(
        "csv_import/confirm.html",
        parsed=parsed,
        payment_account=payment_account,
        default_expense_id=default_expense.code if default_expense else 0,
        default_income_id=default_income.code if default_income else 0,
        grouped_accounts=grouped_accounts,
        restricted_before_year=restricted_before,
        closed_periods=closed_periods,
        has_ai_config=has_ai_config,
        capital_code=capital_code,
    )


@bp.route("/reconcile", methods=["POST"])
@login_required
def reconcile():
    """照合API — Alpine.jsからfetchで呼び出し"""
    from app.services.reconciliation import find_matches

    data_key = session.get("csv_data_key")
    payment_account_code = session.get("csv_payment_account_code")
    parsed = load_import_data(data_key)
    if not parsed or not payment_account_code:
        return jsonify({"error": "データがありません"}), 400

    results = find_matches(get_effective_user_id(), payment_account_code, parsed)
    return jsonify(results)


@bp.route("/ai-reconcile-context", methods=["GET"])
@login_required
@limiter.limit("60 per hour")
def ai_reconcile_context():
    """AI 照合のためのプロンプト材料 + 照合候補データを返す。

    LLM 呼出はクライアント側 reconcile_orchestrator.js が行う。サーバには
    LLM 出力 (matches) は通知不要 (クライアント UI で直接表示する)。
    """
    from app.services.reconciliation import (
        AI_RECONCILE_PROMPT_TEMPLATE, AI_RECONCILE_BATCH_SIZE, find_matches,
    )
    from app.services.ai_receipt import PROVIDER_DEFAULTS
    from app.models.ai_config import UserAIConfig

    data_key = session.get("csv_data_key")
    payment_account_code = session.get("csv_payment_account_code")
    parsed = load_import_data(data_key)
    if not parsed or not payment_account_code:
        return jsonify({"error": "データがありません"}), 400

    user_id = get_effective_user_id()
    results = find_matches(user_id, payment_account_code, parsed)

    unmatched_csv = []
    for r in results["csv_results"]:
        if r["status"] == "unmatched":
            csv = parsed[r["csv_index"]]
            amount = int(csv.get("withdrawal") or 0) or int(csv.get("deposit") or 0)
            if amount:
                unmatched_csv.append({
                    "csv_index": r["csv_index"],
                    "date": csv.get("date", ""),
                    "description": csv.get("description", ""),
                    "amount": amount,
                })

    config = UserAIConfig.query.filter_by(user_id=user_id).first()
    custom_prompt = config.custom_prompt if config else ""

    return jsonify({
        "ok": True,
        "prompt_template": AI_RECONCILE_PROMPT_TEMPLATE,
        "batch_size": AI_RECONCILE_BATCH_SIZE,
        "unmatched_csv": unmatched_csv,
        "journal_candidates": results["journal_only"],
        "custom_prompt": custom_prompt,
        "default_model_by_provider": {
            k: v for k, v in PROVIDER_DEFAULTS.items() if k != "llama_cpp"
        },
    })


@bp.route("/match/snap-date", methods=["POST"])
@login_required
def snap_match_date():
    """日付ズレ照合の修正アクション: 仕訳の日付を CSV 日付に合わせる。

    レシート起票時の日付と CSV 上の計上日がズレているケース（クレジットカードの
    利用日 vs 計上日）で、ユーザーが「これは同じ取引」と判断したときに使う。
    結果として `date_band: "exact"` に変わり、再照合不要で表示が更新される。
    """
    from datetime import date as _date
    from flask import current_app

    payload = request.get_json(silent=True) or {}
    entry_id = payload.get("entry_id")
    csv_date_str = payload.get("csv_date")

    if not entry_id or not csv_date_str:
        return jsonify({"error": "entry_id と csv_date が必要です"}), 400

    try:
        csv_date = _date.fromisoformat(str(csv_date_str))
    except (ValueError, TypeError):
        return jsonify({"error": "CSV 日付の形式が不正です"}), 400

    user_id = get_effective_user_id()
    entry = JournalEntry.query.filter_by(id=entry_id, user_id=user_id).first()
    if entry is None:
        return jsonify({"error": "仕訳が見つかりません"}), 404

    # 移動元（現在の仕訳）が変更可能か
    err = check_entry_modifiable(user_id, entry)
    if err:
        return jsonify({"error": err}), 400

    # 移動先の期間がオープンか（同じ仕訳でも別月に移すとロック中の月かもしれない）
    err = check_period_open_for_new(user_id, csv_date.year, csv_date.month)
    if err:
        return jsonify({"error": err}), 400

    old_date = entry.date.isoformat()
    entry.date = csv_date
    db.session.commit()
    current_app.logger.info(
        "snap_match_date: entry_id=%s user=%s %s -> %s",
        entry_id, user_id, old_date, csv_date.isoformat(),
    )

    return jsonify({
        "success": True,
        "entry_id": entry_id,
        "new_date": csv_date.isoformat(),
        "date_diff_days": 0,
        "date_band": "exact",
    })
