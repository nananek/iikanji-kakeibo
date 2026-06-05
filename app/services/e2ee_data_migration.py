"""E7 一斉移行: v4.0.0 平文データの temp-MK サーバ側暗号化 (#114)。

v4.0.0 (alembic 045) の平文を E2EE 化するため、サーバが利用者ごとの一時マスター鍵
(temp-MK, ``users.migration_temp_mk``) で各行を暗号化し ``encrypted_blob`` /
``blob_iv`` に格納する。生成する暗号文はクライアント (Web / client-py) が復号できる
完全互換フォーマット ([[migration_crypto]] 参照)。

実行タイミング (重要): スキーマ追加 (050 で blob 列, 054 で is_closing/fiscal_month)
の後・平文列 DROP (055 以降) の **前**、すなわち **alembic revision 054** の状態で
実行する。055 以降の破壊的ドロップにはガード (#114) があり、本暗号化を飛ばすと停止
する。本モジュールは平文列を raw SQL で読むため、055 で平文列が DROP された後は
実行できない (precheck で検出)。

移行完了後は各利用者がクライアントで自分の MK へ再ラップし temp_mk をクリアする
(別フェーズ)。本モジュールは冪等: encrypted_blob が既に埋まっている行はスキップする。
"""

from __future__ import annotations

import os

from sqlalchemy import text

from app.services.migration_crypto import build_aad, encrypt_record

_EMPTY = "(encrypted_blob IS NULL OR octet_length(encrypted_blob)=0)"


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c"
            ),
            {"t": table, "c": column},
        ).first()
    )


def _ensure_temp_mk(conn, user_id: int) -> bytes:
    """user の temp-MK を取得。未設定なら 32B 乱数を生成して保存 (冪等)。"""
    row = conn.execute(
        text("SELECT migration_temp_mk FROM users WHERE id=:uid"),
        {"uid": user_id},
    ).first()
    if row is not None and row[0]:
        mk = bytes(row[0])
        if len(mk) == 32:
            return mk
    mk = os.urandom(32)
    conn.execute(
        text("UPDATE users SET migration_temp_mk=:mk WHERE id=:uid"),
        {"mk": mk, "uid": user_id},
    )
    return mk


def _migrate_journal_entries(conn, user_id: int, mk: bytes) -> int:
    aad = build_aad("je", user_id)
    rows = conn.execute(
        text(
            "SELECT id, date, description, source, fiscal_period "
            "FROM journal_entries WHERE user_id=:uid AND " + _EMPTY
        ),
        {"uid": user_id},
    ).fetchall()
    for r in rows:
        record = {
            "v": 1,
            "date": r.date.isoformat() if r.date else None,
            "description": r.description or "",
            "source": r.source or "journal",
            "fiscal_period": r.fiscal_period,
        }
        blob, iv = encrypt_record(mk, record, aad)
        conn.execute(
            text(
                "UPDATE journal_entries SET encrypted_blob=:b, blob_iv=:iv WHERE id=:id"
            ),
            {"b": blob, "iv": iv, "id": r.id},
        )
    return len(rows)


def _migrate_journal_entry_lines(conn, user_id: int, mk: bytes) -> int:
    aad = build_aad("jel", user_id)
    rows = conn.execute(
        text(
            "SELECT jel.id AS id, jel.account_code AS account_code, "
            "jel.debit_amount AS debit_amount, jel.credit_amount AS credit_amount, "
            "jel.description AS description "
            "FROM journal_entry_lines jel "
            "JOIN journal_entries je ON je.id = jel.journal_entry_id "
            "WHERE je.user_id=:uid AND (jel.encrypted_blob IS NULL "
            "OR octet_length(jel.encrypted_blob)=0)"
        ),
        {"uid": user_id},
    ).fetchall()
    for r in rows:
        record = {
            "v": 1,
            "account_code": r.account_code or "",
            "debit_amount": int(r.debit_amount or 0),
            "credit_amount": int(r.credit_amount or 0),
            "description": r.description or "",
        }
        blob, iv = encrypt_record(mk, record, aad)
        conn.execute(
            text(
                "UPDATE journal_entry_lines SET encrypted_blob=:b, blob_iv=:iv "
                "WHERE id=:id"
            ),
            {"b": blob, "iv": iv, "id": r.id},
        )
    return len(rows)


def _migrate_medical_expenses(conn, user_id: int, mk: bytes) -> int:
    aad = build_aad("me", user_id)
    rows = conn.execute(
        text(
            "SELECT id, date, patient_name, hospital_name, treatment_description, "
            "provider_type, amount_paid, insurance_reimbursement "
            "FROM medical_expenses WHERE user_id=:uid AND " + _EMPTY
        ),
        {"uid": user_id},
    ).fetchall()
    for r in rows:
        record = {
            "v": 1,
            "date": r.date.isoformat() if r.date else None,
            "patient_name": r.patient_name or "",
            "hospital_name": r.hospital_name or "",
            "treatment_description": r.treatment_description or "",
            "provider_type": r.provider_type or None,
            "amount_paid": int(r.amount_paid or 0),
            "insurance_reimbursement": int(r.insurance_reimbursement or 0),
        }
        blob, iv = encrypt_record(mk, record, aad)
        conn.execute(
            text(
                "UPDATE medical_expenses SET encrypted_blob=:b, blob_iv=:iv WHERE id=:id"
            ),
            {"b": blob, "iv": iv, "id": r.id},
        )
    return len(rows)


def migrate_all_to_e2ee(db, *, user_id: int | None = None) -> dict:
    """全 (または指定) 利用者の平文台帳データを temp-MK で暗号化する。

    Returns: 暗号化件数の集計 dict。
    Raises: RuntimeError — 平文列が既に DROP 済 (revision >= 055) で実行不可。
    """
    conn = db.session.connection()

    # precheck: 平文列が残っているか (revision 054 でのみ実行可能)。
    required = [
        ("journal_entries", "date"),
        ("journal_entry_lines", "account_code"),
        ("medical_expenses", "patient_name"),
    ]
    missing = [f"{t}.{c}" for t, c in required if not _column_exists(conn, t, c)]
    if missing:
        raise RuntimeError(
            "平文列が見つかりません (" + ", ".join(missing) + ")。E7 データ暗号化は "
            "alembic revision 054 (平文列 DROP 前) の状態で実行してください。"
            "既に 055 以降へ進んでいる場合、平文は失われています。"
        )

    if user_id is not None:
        user_ids = [user_id]
    else:
        user_ids = [
            r[0] for r in conn.execute(text("SELECT id FROM users ORDER BY id"))
        ]

    totals = {"users": 0, "journal_entries": 0, "journal_entry_lines": 0,
              "medical_expenses": 0}
    for uid in user_ids:
        mk = _ensure_temp_mk(conn, uid)
        totals["journal_entries"] += _migrate_journal_entries(conn, uid, mk)
        totals["journal_entry_lines"] += _migrate_journal_entry_lines(conn, uid, mk)
        totals["medical_expenses"] += _migrate_medical_expenses(conn, uid, mk)
        totals["users"] += 1

    db.session.commit()
    return totals
