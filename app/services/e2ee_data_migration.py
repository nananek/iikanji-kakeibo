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

テスト方針: record 構築 (je_record / jel_record / me_record) は純粋関数として
ユニットテスト。DB グルー (information_schema / octet_length / raw SQL) は PostgreSQL
専用で SQLite フィクスチャでは動かないため、実 PG での往復検証
(docs/v5-e2ee/e7-migration-runbook.md) で担保し、カバレッジ上は除外する。
"""

from __future__ import annotations

from app.services.migration_crypto import build_aad, encrypt_record

# 暗号文が未設定 (NULL or 空) の行 = 未暗号化。冪等フィルタ兼ガード条件。
_UNENCRYPTED = "(encrypted_blob IS NULL OR octet_length(encrypted_blob)=0)"


# --- 平文行 → record dict (純粋関数。クライアントの暗号化前 record と同形状) ---

def je_record(date, description, source, fiscal_period) -> dict:
    """journal_entries の平文列から je record を構築 (entries_builder.js と同形状)。"""
    return {
        "v": 1,
        "date": date.isoformat() if date else None,
        "description": description or "",
        "source": source or "journal",
        "fiscal_period": fiscal_period,
    }


def jel_record(account_code, debit_amount, credit_amount, description) -> dict:
    """journal_entry_lines の平文列から jel record を構築。"""
    return {
        "v": 1,
        "account_code": account_code or "",
        "debit_amount": int(debit_amount or 0),
        "credit_amount": int(credit_amount or 0),
        "description": description or "",
    }


def me_record(date, patient_name, hospital_name, treatment_description,
              provider_type, amount_paid, insurance_reimbursement) -> dict:
    """medical_expenses の平文列から me record を構築。"""
    return {
        "v": 1,
        "date": date.isoformat() if date else None,
        "patient_name": patient_name or "",
        "hospital_name": hospital_name or "",
        "treatment_description": treatment_description or "",
        "provider_type": provider_type or None,
        "amount_paid": int(amount_paid or 0),
        "insurance_reimbursement": int(insurance_reimbursement or 0),
    }


def vmeta_record(original_filename, image_mime) -> dict:
    """vouchers の平文メタから vmeta record を構築 (voucher_upload.js と同形状)。"""
    return {
        "v": 1,
        "original_filename": original_filename or "",
        "image_mime": image_mime or "application/octet-stream",
    }


# --- DB グルー (PostgreSQL 専用。カバレッジ除外、実 PG で往復検証) ---

def _column_exists(conn, table, column):  # pragma: no cover
    from sqlalchemy import text
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c"
            ),
            {"t": table, "c": column},
        ).first()
    )


def _ensure_temp_mk(conn, user_id):  # pragma: no cover
    import os

    from sqlalchemy import text
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


def _migrate_journal_entries(conn, user_id, mk):  # pragma: no cover
    from sqlalchemy import text
    aad = build_aad("je", user_id)
    rows = conn.execute(
        text(
            "SELECT id, date, description, source, fiscal_period "
            "FROM journal_entries WHERE user_id=:uid AND "
            "(encrypted_blob IS NULL OR octet_length(encrypted_blob)=0)"
        ),
        {"uid": user_id},
    ).fetchall()
    for r in rows:
        record = je_record(r.date, r.description, r.source, r.fiscal_period)
        blob, iv = encrypt_record(mk, record, aad)
        conn.execute(
            text(
                "UPDATE journal_entries SET encrypted_blob=:b, blob_iv=:iv WHERE id=:id"
            ),
            {"b": blob, "iv": iv, "id": r.id},
        )
    return len(rows)


def _migrate_journal_entry_lines(conn, user_id, mk):  # pragma: no cover
    from sqlalchemy import text
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
        record = jel_record(
            r.account_code, r.debit_amount, r.credit_amount, r.description
        )
        blob, iv = encrypt_record(mk, record, aad)
        conn.execute(
            text(
                "UPDATE journal_entry_lines SET encrypted_blob=:b, blob_iv=:iv "
                "WHERE id=:id"
            ),
            {"b": blob, "iv": iv, "id": r.id},
        )
    return len(rows)


def _migrate_medical_expenses(conn, user_id, mk):  # pragma: no cover
    from sqlalchemy import text
    aad = build_aad("me", user_id)
    rows = conn.execute(
        text(
            "SELECT id, date, patient_name, hospital_name, treatment_description, "
            "provider_type, amount_paid, insurance_reimbursement "
            "FROM medical_expenses WHERE user_id=:uid AND "
            "(encrypted_blob IS NULL OR octet_length(encrypted_blob)=0)"
        ),
        {"uid": user_id},
    ).fetchall()
    for r in rows:
        record = me_record(
            r.date, r.patient_name, r.hospital_name, r.treatment_description,
            r.provider_type, r.amount_paid, r.insurance_reimbursement,
        )
        blob, iv = encrypt_record(mk, record, aad)
        conn.execute(
            text(
                "UPDATE medical_expenses SET encrypted_blob=:b, blob_iv=:iv WHERE id=:id"
            ),
            {"b": blob, "iv": iv, "id": r.id},
        )
    return len(rows)


def migrate_all_to_e2ee(db, *, user_id=None):  # pragma: no cover
    """全 (または指定) 利用者の平文台帳データを temp-MK で暗号化する (冪等)。

    リビジョン対応: 平文列が残っているテーブル群のみ暗号化する。
    - 仕訳/医療費は revision 054 (平文残存) で暗号化 → 055 で平文 DROP。
    - 証憑は revision 056 (encrypted_meta_blob/aad_id 追加・original_filename 残存) で
      暗号化 → 057/060 で平文 DROP。
    2 パス (054 で本コマンド → 055/056 へ upgrade → 056 で再度本コマンド) を runbook
    が定める。既に暗号化済 (blob 非空) の行はスキップするので再実行は安全。

    Returns: 暗号化件数の集計 dict。
    Raises: RuntimeError — 暗号化対象が一切無い (誤ったタイミング/列なし) 場合。
    """
    from sqlalchemy import text

    from app.services.storage import get_storage_backend
    conn = db.session.connection()

    if user_id is not None:
        user_ids = [user_id]
    else:
        user_ids = [
            r[0] for r in conn.execute(text("SELECT id FROM users ORDER BY id"))
        ]

    # どのテーブル群が暗号化可能か (平文列 + 暗号文列が共存するか) を判定。
    ledger_ready = _column_exists(conn, "journal_entries", "date")
    voucher_ready = (
        _column_exists(conn, "vouchers", "original_filename")
        and _column_exists(conn, "vouchers", "encrypted_meta_blob")
        and _column_exists(conn, "vouchers", "aad_id")
    )
    if not ledger_ready and not voucher_ready:
        raise RuntimeError(
            "暗号化可能な平文列がありません。仕訳/医療費は revision 054、証憑は "
            "revision 056 の状態で実行してください (docs/v5-e2ee/e7-migration-runbook.md)。"
        )

    storage = get_storage_backend() if voucher_ready else None
    totals = {"users": 0, "journal_entries": 0, "journal_entry_lines": 0,
              "medical_expenses": 0, "vouchers": 0, "voucher_audit_logs": 0}
    for uid in user_ids:
        mk = _ensure_temp_mk(conn, uid)
        if ledger_ready:
            totals["journal_entries"] += _migrate_journal_entries(conn, uid, mk)
            totals["journal_entry_lines"] += _migrate_journal_entry_lines(conn, uid, mk)
            totals["medical_expenses"] += _migrate_medical_expenses(conn, uid, mk)
        if voucher_ready:
            totals["vouchers"] += _migrate_vouchers(conn, uid, mk, storage)
            totals["voucher_audit_logs"] += _migrate_voucher_audit_logs(conn, uid, mk)
        totals["users"] += 1

    db.session.commit()
    return totals


# --- 証憑 (画像/サムネ/メタ/監査ログ) の暗号化 (PostgreSQL + ストレージ専用) ---

def _random_aad_id():  # pragma: no cover
    import secrets
    # voucher.py:_random_aad_id と同じく 1..2^63-1 の 63bit ランダム。
    return secrets.randbelow((1 << 63) - 1) + 1


def _migrate_vouchers(conn, user_id, mk, storage):  # pragma: no cover
    import hashlib

    from sqlalchemy import text

    from app.services.migration_crypto import build_aad, encrypt_blob, encrypt_record
    from app.services.storage import (
        ENCRYPTED_CONTENT_TYPE,
        generate_thumbnail,
        make_encrypted_thumbnail_key,
        make_storage_key,
        make_thumbnail_key,
    )

    rows = conn.execute(
        text(
            "SELECT id, image_key, image_mime, original_filename, aad_id "
            "FROM vouchers WHERE user_id=:uid AND image_key<>'' "
            "AND (encrypted_meta_blob IS NULL OR octet_length(encrypted_meta_blob)=0)"
        ),
        {"uid": user_id},
    ).fetchall()
    n = 0
    for r in rows:
        plain = storage.get(r.image_key)
        aad_id = r.aad_id
        if not aad_id:
            aad_id = _random_aad_id()
            conn.execute(
                text("UPDATE vouchers SET aad_id=:a WHERE id=:id"),
                {"a": aad_id, "id": r.id},
            )
        img_blob = encrypt_blob(mk, plain, build_aad("vimg", user_id, aad_id))
        thumb = generate_thumbnail(plain, max_size=200)
        thumb_blob = encrypt_blob(mk, thumb, build_aad("vthumb", user_id, aad_id))
        meta_blob, meta_iv = encrypt_record(
            mk, vmeta_record(r.original_filename, r.image_mime),
            build_aad("vmeta", user_id, aad_id),
        )
        new_key = make_storage_key(user_id, r.id, ENCRYPTED_CONTENT_TYPE)
        thumb_key = make_encrypted_thumbnail_key(new_key)
        storage.put(new_key, img_blob, ENCRYPTED_CONTENT_TYPE)
        storage.put(thumb_key, thumb_blob, ENCRYPTED_CONTENT_TYPE)
        file_hash_cipher = hashlib.sha256(img_blob).hexdigest()
        file_hash_plain = hashlib.sha256(plain).hexdigest()
        conn.execute(
            text(
                "UPDATE vouchers SET encrypted_meta_blob=:mb, meta_iv=:mi, "
                "file_hash_plain=:fhp, file_hash=:fhc, thumbnail_key=:tk, "
                "image_key=:ik WHERE id=:id"
            ),
            {"mb": meta_blob, "mi": meta_iv, "fhp": file_hash_plain,
             "fhc": file_hash_cipher, "tk": thumb_key, "ik": new_key, "id": r.id},
        )
        # 旧平文の本体画像と旧サーバ生成サムネ (_thumb.jpg) を削除する。
        # プライバシー上、平文画像を残さない (E7 の目的)。
        for old in (r.image_key, make_thumbnail_key(r.image_key)):
            if old and old != new_key and old != thumb_key:
                try:
                    storage.delete(old)
                except Exception:
                    pass  # 削除失敗は移行を止めない (容量のみの問題)
        n += 1
    return n


def _migrate_voucher_audit_logs(conn, user_id, mk):  # pragma: no cover
    import json

    from sqlalchemy import text

    from app.services.migration_crypto import build_aad, encrypt_record

    # 監査ログ detail (主にサーバ生成の orphaned/hash イベント JSON)。voucher の
    # aad_id を AAD id に使う (vmeta 等と同ドメイン)。detail 平文を parse して record 化。
    rows = conn.execute(
        text(
            "SELECT val.id AS id, val.detail AS detail, v.aad_id AS aad_id "
            "FROM voucher_audit_logs val JOIN vouchers v ON v.id=val.voucher_id "
            "WHERE val.user_id=:uid AND val.detail IS NOT NULL AND val.detail<>'' "
            "AND (val.encrypted_detail_blob IS NULL "
            "OR octet_length(val.encrypted_detail_blob)=0)"
        ),
        {"uid": user_id},
    ).fetchall()
    n = 0
    for r in rows:
        if not r.aad_id:
            continue  # 親 voucher が未移行 (aad_id なし) の場合はスキップ
        try:
            detail_obj = json.loads(r.detail)
        except (ValueError, TypeError):
            detail_obj = {"raw": r.detail}
        blob, iv = encrypt_record(
            mk, detail_obj, build_aad("valog", user_id, r.aad_id)
        )
        conn.execute(
            text(
                "UPDATE voucher_audit_logs SET encrypted_detail_blob=:b, "
                "detail_iv=:iv WHERE id=:id"
            ),
            {"b": blob, "iv": iv, "id": r.id},
        )
        n += 1
    return n
