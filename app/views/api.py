"""外部 API (Bearer APIキー認証)"""

import functools
from datetime import date as date_type, datetime, timezone

from flask import Blueprint, jsonify, request, g

from app.extensions import db
from app.models.api_key import APIKey
from app.services.accounting import create_journal_entry
from app.services.audit import get_submitted_account_ids
from app.services.fiscal import check_period_open_for_new

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def api_key_required(f):
    """Authorization: Bearer ik_xxx ヘッダーで認証するデコレータ"""

    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Authorization ヘッダーが必要です。"}), 401

        raw_key = auth[7:]
        key_hash = APIKey.hash_key(raw_key)
        api_key = APIKey.query.filter_by(key_hash=key_hash, is_active=True).first()
        if not api_key:
            return jsonify({"error": "無効な API キーです。"}), 401

        api_key.last_used_at = datetime.now(timezone.utc)
        db.session.commit()

        g.api_user_id = api_key.user_id
        return f(*args, **kwargs)

    return decorated


@bp.route("/journals", methods=["POST"])
@api_key_required
def create_journal():
    """仕訳起票 API"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON ボディが必要です。"}), 400

    # バリデーション
    date_str = data.get("date")
    description = (data.get("description") or "").strip()
    lines = data.get("lines")
    source = data.get("source", "api")

    if not date_str:
        return jsonify({"error": "date は必須です。"}), 400
    if not description:
        return jsonify({"error": "description は必須です。"}), 400
    if not lines or not isinstance(lines, list):
        return jsonify({"error": "lines は必須です（配列）。"}), 400

    try:
        entry_date = date_type.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "date の形式が不正です（YYYY-MM-DD）。"}), 400

    user_id = g.api_user_id

    # 確定済み期間チェック
    err = check_period_open_for_new(user_id, entry_date.year, entry_date.month)
    if err:
        return jsonify({"error": err}), 400

    # lines_data 変換
    lines_data = []
    for i, line in enumerate(lines):
        account_id = line.get("account_id")
        if not account_id:
            return jsonify({"error": f"lines[{i}].account_id は必須です。"}), 400
        lines_data.append({
            "account_id": int(account_id),
            "debit_amount": int(line.get("debit", 0) or 0),
            "credit_amount": int(line.get("credit", 0) or 0),
            "description": line.get("description", ""),
        })

    # 提出済みロック科目チェック
    locked_ids = get_submitted_account_ids(user_id)
    if locked_ids:
        used_ids = {ld["account_id"] for ld in lines_data}
        if used_ids & locked_ids:
            return jsonify({"error": "提出済みの税務科目を含むため登録できません。"}), 400

    try:
        entry = create_journal_entry(
            user_id=user_id,
            date=entry_date,
            description=description,
            lines_data=lines_data,
            source=source,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({
        "ok": True,
        "id": entry.id,
        "entry_number": entry.entry_number,
    }), 201
