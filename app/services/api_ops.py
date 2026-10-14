"""REST API / MCP サーバーで共有する業務操作。

Flask の ``request`` に依存しない形で API のコアロジックをまとめ、
REST ビュー (``app/views/api.py``) と MCP ツール (``app/views/mcp.py``)
の双方から呼び出す。バリデーション失敗は :class:`ApiError` で表現する。
"""

import json
from datetime import date as date_type

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.ai_draft import AIDraft
from app.models.journal import JournalEntry
from app.models.voucher import Voucher
from app.services.accounting import create_journal_entry
from app.services.audit import (
    get_submitted_account_codes,
    is_entry_locked_for_owner,
)
from app.services.clock import today as app_today
from app.services.fiscal import check_entry_modifiable, check_period_open_for_new
from app.services.storage import get_storage_backend, make_thumbnail_key
from app.views.helpers import safe_user_error


class ApiError(Exception):
    """API / MCP 共通のエラー。``status`` は HTTP ステータス相当。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# --- シリアライザ (REST / MCP 共用) ---


def entry_to_dict(entry: JournalEntry) -> dict:
    """JournalEntry を API レスポンス用 dict に変換"""
    return {
        "id": entry.id,
        "date": entry.date.isoformat(),
        "entry_number": entry.entry_number,
        "description": entry.description,
        "source": entry.source,
        "lines": [
            {
                "account_code": line.account_code,
                "debit": int(line.debit_amount or 0),
                "credit": int(line.credit_amount or 0),
                "description": line.description or "",
            }
            for line in entry.lines
        ],
        "vouchers": [
            {
                "id": v.id,
                "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
            }
            for v in entry.vouchers
        ],
    }


def draft_to_dict(draft: AIDraft, include_suggestions: bool = False) -> dict:
    """AIDraft を API レスポンス用 dict に変換"""
    result: dict = {
        "id": draft.id,
        "status": draft.status,
        "comment": draft.comment or "",
        "created_at": draft.created_at.isoformat(),
    }
    if draft.suggestions_json:
        try:
            suggestions = json.loads(draft.suggestions_json)
            if suggestions:
                s = suggestions[0]
                result["summary"] = {
                    "title": s.get("title", ""),
                    "date": s.get("date", ""),
                    "description": s.get("entry_description", ""),
                    "amount": sum(
                        l.get("debit_amount", 0) for l in s.get("lines", [])
                    ),
                    "suggestion_count": len(suggestions),
                }
            if include_suggestions:
                result["suggestions"] = suggestions
        except (json.JSONDecodeError, IndexError):
            pass
    return result


# --- 仕訳 ---


def list_journals(user_id, *, page=1, per_page=20, date_from=None, date_to=None):
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or 20), 1), 100)

    query = JournalEntry.query.filter_by(user_id=user_id)

    if date_from:
        try:
            query = query.filter(
                JournalEntry.date >= date_type.fromisoformat(date_from)
            )
        except ValueError:
            raise ApiError("date_from の形式が不正です（YYYY-MM-DD）。")

    if date_to:
        try:
            query = query.filter(
                JournalEntry.date <= date_type.fromisoformat(date_to)
            )
        except ValueError:
            raise ApiError("date_to の形式が不正です（YYYY-MM-DD）。")

    total = query.count()
    entries = (
        query.order_by(JournalEntry.date.desc(), JournalEntry.entry_number.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {
        "ok": True,
        "journals": [entry_to_dict(e) for e in entries],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def get_journal(user_id, entry_id):
    entry = JournalEntry.query.filter_by(id=entry_id, user_id=user_id).first()
    if not entry:
        raise ApiError("仕訳が見つかりません。", 404)
    return {"ok": True, "journal": entry_to_dict(entry)}


def create_journal(user_id, data: dict) -> JournalEntry:
    """仕訳を起票して JournalEntry を返す。draft_id の処理は呼び出し側。"""
    if not data:
        raise ApiError("JSON ボディが必要です。")

    date_str = data.get("date")
    description = (data.get("description") or "").strip()
    lines = data.get("lines")
    source = data.get("source", "api")

    if not date_str:
        raise ApiError("date は必須です。")
    if not description:
        raise ApiError("description は必須です。")
    if not lines or not isinstance(lines, list):
        raise ApiError("lines は必須です（配列）。")

    try:
        entry_date = date_type.fromisoformat(date_str)
    except ValueError:
        raise ApiError("date の形式が不正です（YYYY-MM-DD）。")

    err = check_period_open_for_new(user_id, entry_date.year, entry_date.month)
    if err:
        raise ApiError(err)

    lines_data = []
    for i, line in enumerate(lines):
        account_code = line.get("account_code")
        if not account_code:
            raise ApiError(f"lines[{i}].account_code は必須です。")
        lines_data.append({
            "account_code": account_code,
            "debit_amount": int(line.get("debit", 0) or 0),
            "credit_amount": int(line.get("credit", 0) or 0),
            "description": line.get("description", ""),
        })

    locked_codes = get_submitted_account_codes(user_id)
    if locked_codes:
        used_codes = {ld["account_code"] for ld in lines_data}
        if used_codes & locked_codes:
            raise ApiError("提出済みの税務科目を含むため登録できません。")

    try:
        entry = create_journal_entry(
            user_id=user_id,
            date=entry_date,
            description=description,
            lines_data=lines_data,
            source=source,
        )
    except ValueError as e:
        raise ApiError(safe_user_error(e))
    return entry


def delete_journal(user_id, entry_id) -> None:
    entry = JournalEntry.query.filter_by(id=entry_id, user_id=user_id).first()
    if not entry:
        raise ApiError("仕訳が見つかりません。", 404)

    if is_entry_locked_for_owner(user_id, entry):
        raise ApiError("提出済みの税務科目を含む伝票のため削除できません。")

    err = check_entry_modifiable(user_id, entry)
    if err:
        raise ApiError(err)

    db.session.delete(entry)
    db.session.commit()


# --- 証憑 ---


def list_vouchers(user_id, *, page=1, per_page=20, date_from=None, date_to=None,
                  search=None, amount_from=None, amount_to=None):
    from sqlalchemy import func
    from app.models.journal import JournalEntryLine

    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or 20), 1), 100)

    query = (
        Voucher.query
        .outerjoin(JournalEntry, Voucher.journal_entry_id == JournalEntry.id)
        .filter(Voucher.user_id == user_id)
    )

    if date_from:
        try:
            d = date_type.fromisoformat(date_from)
        except ValueError:
            raise ApiError("date_from の形式が不正です。")
        query = query.filter(
            db.or_(
                JournalEntry.date >= d,
                db.and_(Voucher.journal_entry_id.is_(None),
                        func.date(Voucher.uploaded_at) >= d),
            )
        )

    if date_to:
        try:
            d = date_type.fromisoformat(date_to)
        except ValueError:
            raise ApiError("date_to の形式が不正です。")
        query = query.filter(
            db.or_(
                JournalEntry.date <= d,
                db.and_(Voucher.journal_entry_id.is_(None),
                        func.date(Voucher.uploaded_at) <= d),
            )
        )

    if search:
        query = query.filter(JournalEntry.description.ilike(f"%{search}%"))

    if amount_from is not None or amount_to is not None:
        amount_subq = (
            db.session.query(
                JournalEntryLine.journal_entry_id,
                func.sum(JournalEntryLine.debit_amount).label("total"),
            )
            .group_by(JournalEntryLine.journal_entry_id)
            .subquery()
        )
        query = query.outerjoin(
            amount_subq, JournalEntry.id == amount_subq.c.journal_entry_id
        )
        if amount_from is not None:
            query = query.filter(amount_subq.c.total >= amount_from)
        if amount_to is not None:
            query = query.filter(amount_subq.c.total <= amount_to)

    total = query.count()
    vouchers = (
        query.order_by(
            func.coalesce(JournalEntry.date, func.date(Voucher.uploaded_at)).desc(),
            Voucher.id.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    result = []
    for v in vouchers:
        entry = v.journal_entry
        d = {
            "id": v.id,
            "journal_entry_id": v.journal_entry_id,
            "image_mime": v.image_mime,
            "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
        }
        if entry:
            d["journal"] = {
                "date": entry.date.isoformat(),
                "description": entry.description,
                "amount": int(entry.total_debit),
            }
        else:
            d["journal"] = None
        result.append(d)

    return {
        "ok": True,
        "vouchers": result,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def get_voucher_image(user_id, voucher_id, *, thumb=False):
    """証憑画像のバイト列と MIME を返す (MCP の画像返却用)。"""
    voucher = Voucher.query.filter_by(id=voucher_id, user_id=user_id).first()
    if not voucher:
        raise ApiError("証憑が見つかりません。", 404)

    backend = get_storage_backend()
    key = voucher.image_key
    mime = voucher.image_mime
    if thumb:
        thumb_key = make_thumbnail_key(voucher.image_key)
        try:
            if backend.exists(thumb_key):
                key, mime = thumb_key, "image/jpeg"
        except Exception:
            pass
    try:
        data = backend.get(key)
    except (FileNotFoundError, OSError):
        raise ApiError("画像ファイルが見つかりません。", 404)
    return data, mime


# --- AI 下書き ---


def list_ai_drafts(user_id, *, status="analyzed", page=1, per_page=50):
    if status not in ("analyzed", "done", "all"):
        raise ApiError("status は analyzed / done / all のいずれかです。")
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or 50), 1), 100)

    query = AIDraft.query.filter_by(user_id=user_id)
    if status != "all":
        query = query.filter_by(status=status)
    else:
        query = query.filter(AIDraft.status.in_(["analyzed", "done"]))

    total = query.count()
    drafts = (
        query.order_by(AIDraft.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {
        "ok": True,
        "drafts": [draft_to_dict(d) for d in drafts],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def get_ai_draft(user_id, draft_id):
    draft = AIDraft.query.filter_by(id=draft_id, user_id=user_id).first()
    if not draft or draft.status == "temp":
        raise ApiError("下書きが見つかりません。", 404)
    return {"ok": True, "draft": draft_to_dict(draft, include_suggestions=True)}


# --- 勘定科目 ---


def list_accounts(user_id, *, include_inactive=False):
    types = AccountType.query.order_by(AccountType.display_order).all()
    type_map = {t.id: t for t in types}
    query = Account.query.filter_by(user_id=user_id)
    if not include_inactive:
        query = query.filter_by(is_active=True)
    accounts = query.order_by(Account.code).all()
    return {
        "ok": True,
        "accounts": [
            {
                "code": a.code,
                "name": a.name,
                "type_code": type_map[a.account_type_id].code
                if a.account_type_id in type_map else None,
                "type_name": type_map[a.account_type_id].name
                if a.account_type_id in type_map else None,
                "normal_balance": type_map[a.account_type_id].normal_balance
                if a.account_type_id in type_map else None,
                "is_active": bool(a.is_active),
                "tax_category": a.tax_category,
                "cost_type": a.cost_type,
                "system_role": a.system_role,
            }
            for a in accounts
        ],
    }


# --- レポート ---


def trial_balance(user_id, *, year=None, period_from=0, period_to=15):
    from app.services.reports import compute_trial_balance

    year = int(year) if year else app_today().year
    pf = int(period_from) if period_from is not None else 0
    pt = int(period_to) if period_to is not None else 15
    balances = compute_trial_balance(user_id, year, pf, pt)
    return {
        "ok": True,
        "year": year,
        "period_from": max(0, min(16, pf)),
        "period_to": max(pf, min(16, pt)),
        "balances": balances,
    }


def income_statement(user_id, *, year=None, month=None):
    from app.services.reports import compute_income_statement

    year = int(year) if year else app_today().year
    if month is not None:
        month = int(month)
        if not (1 <= month <= 12):
            raise ApiError("month は 1-12 で指定してください。")
    data = compute_income_statement(user_id, year, month)
    return {"ok": True, "year": year, "month": month, **data}


def monthly_comparison(user_id, *, year=None):
    from app.services.tax import get_monthly_comparison

    year = int(year) if year else app_today().year
    data = get_monthly_comparison(user_id, year)
    return {
        "ok": True,
        "year": year,
        "expense_accounts": data["expense_accounts"],
        "income_accounts": data["income_accounts"],
        "expense_totals": [int(v) for v in data["expense_totals"]],
        "income_totals": [int(v) for v in data["income_totals"]],
    }


def tax_summary(user_id, *, year=None):
    from app.services.tax import get_tax_summary, get_medical_summary

    year = int(year) if year else app_today().year
    tax = get_tax_summary(user_id, year)
    medical = get_medical_summary(user_id, year)

    tax_serializable = {
        cat: {
            "label": v["label"],
            "total": int(v["total"]),
            "accounts": [
                {"name": a["name"], "amount": int(a["amount"])}
                for a in v["accounts"]
            ],
        }
        for cat, v in tax.items()
    }
    medical_serializable = {
        "total_paid": int(medical.get("total_paid", 0) or 0),
        "total_reimbursed": int(medical.get("total_reimbursed", 0) or 0),
        "net_total": int(medical.get("net_total", 0) or 0),
        "by_patient": [
            {
                "name": p.get("name", ""),
                "paid": int(p.get("paid", 0) or 0),
                "reimbursed": int(p.get("reimbursed", 0) or 0),
                "net": int(p.get("net", 0) or 0),
                "hospitals": [
                    {
                        "name": h.get("name", ""),
                        "paid": int(h.get("paid", 0) or 0),
                        "reimbursed": int(h.get("reimbursed", 0) or 0),
                        "net": int(h.get("net", 0) or 0),
                        "provider_type": h.get("provider_type", ""),
                    }
                    for h in p.get("hospitals", [])
                ],
            }
            for p in medical.get("by_patient", [])
        ],
    }
    return {
        "ok": True,
        "year": year,
        "tax_summary": tax_serializable,
        "medical_summary": medical_serializable,
    }
