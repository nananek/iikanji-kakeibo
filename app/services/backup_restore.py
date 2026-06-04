"""全置換 restore (Phase v5 BU-4b)。

復号済み平文 backup JSON を受け取り、自分の全関連データを delete してから
INSERT で再構築する。1 トランザクション内でアトミック。

含めるテーブル (`/api/v1/backup/export` と同じ shape):
  accounts / fiscal_closes / journal_entries / journal_entry_lines /
  medical_expenses / balance_cache_blobs / vouchers (画像 base64) /
  ai_drafts (画像 base64) / user_ai_config /
  tax_form_mappings / csv_column_profiles

触らないもの (鍵類 / 監査ログ):
  User / APIKey / OAuthToken / OAuthDevice / WebAuthnCredential /
  AuditGrant / AuditGrantAccount / AIUsageLog / StorageUsage
  → 自分のセッションを破壊しないため & backup 対象外

VoucherAuditLog は user_id を NULL 化のみ (電帳法 7 年保管の匿名化保持)。

設計 memo: [[project_v5_backup_restore]]
"""

from __future__ import annotations

import base64
import hashlib
from datetime import date, datetime, timezone
from typing import Any

from flask import current_app
from sqlalchemy import select as sa_select, update

from app.extensions import db
from app.models.account import Account
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.balance_cache import BalanceCacheBlob
from app.models.csv_column_profile import CsvColumnProfile
from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.medical import MedicalExpense
from app.models.tax_form import TaxFormMapping
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.services.storage import (
    ENCRYPTED_CONTENT_TYPE,
    get_storage_backend,
    make_encrypted_thumbnail_key,
    make_storage_key,
    make_thumbnail_key,
    store_image_with_thumbnail,
)


SUPPORTED_VERSION = "1.0"

# data dict 内のテーブル別バリデーション (リスト型必須 / null 許容オブジェクト)
_LIST_TABLES = (
    "accounts", "fiscal_closes", "journal_entries", "journal_entry_lines",
    "medical_expenses", "balance_cache_blobs", "vouchers", "ai_drafts",
    "tax_form_mappings", "csv_column_profiles",
)


class BackupValidationError(ValueError):
    """backup の構造 / 整合性が壊れている (HTTP 400 相当)。"""


class BackupRestoreError(RuntimeError):
    """restore 実行中の致命的な失敗 (HTTP 500 相当)。"""


# --- validation ---


def _validate_backup(user_id: int, backup: Any) -> None:
    if not isinstance(backup, dict):
        raise BackupValidationError("backup must be an object")
    for k in ("version", "user_id", "data"):
        if k not in backup:
            raise BackupValidationError(f"missing key: {k}")
    if backup["version"] != SUPPORTED_VERSION:
        raise BackupValidationError(
            f"unsupported version: {backup['version']!r}"
            f" (expected {SUPPORTED_VERSION!r})",
        )
    # IDOR 防御: backup.user_id が自分でなければ拒否
    if backup["user_id"] != user_id:
        raise BackupValidationError("backup.user_id does not match auth user")
    data = backup["data"]
    if not isinstance(data, dict):
        raise BackupValidationError("backup.data must be an object")
    # 各テーブルの型ガード
    for tbl in _LIST_TABLES:
        v = data.get(tbl)
        if v is not None and not isinstance(v, list):
            raise BackupValidationError(f"{tbl} must be a list or omitted")
    cfg = data.get("user_ai_config")
    if cfg is not None and not isinstance(cfg, dict):
        raise BackupValidationError("user_ai_config must be an object or null")

    # FK 事前検算: account_code 参照
    account_codes = {
        a.get("code") for a in (data.get("accounts") or []) if isinstance(a, dict)
    }
    for tbl in ("journal_entry_lines", "csv_column_profiles", "tax_form_mappings"):
        for row in data.get(tbl) or []:
            if not isinstance(row, dict):
                raise BackupValidationError(f"{tbl}[*] must be an object")
            code = row.get("account_code")
            if code is None:
                continue
            if code not in account_codes:
                raise BackupValidationError(
                    f"{tbl} references unknown account_code: {code!r}",
                )

    # FK 事前検算: journal_entry_id 参照 (null は許容)
    entry_ids = {
        e.get("id") for e in (data.get("journal_entries") or []) if isinstance(e, dict)
    }
    for tbl in ("journal_entry_lines", "medical_expenses", "vouchers"):
        for row in data.get(tbl) or []:
            if not isinstance(row, dict):
                raise BackupValidationError(f"{tbl}[*] must be an object")
            ref = row.get("journal_entry_id")
            if ref is None:
                continue
            if ref not in entry_ids:
                raise BackupValidationError(
                    f"{tbl} references unknown journal_entry_id: {ref}",
                )

    # balance_cache_blobs の year/period 範囲
    for row in data.get("balance_cache_blobs") or []:
        if not isinstance(row, dict):
            raise BackupValidationError("balance_cache_blobs[*] must be an object")
        year = row.get("year")
        period = row.get("period")
        if not isinstance(year, int) or year < 1900 or year > 2200:
            raise BackupValidationError(f"invalid year: {year!r}")
        if not isinstance(period, int) or period < 0 or period > 16:
            raise BackupValidationError(f"invalid period: {period!r}")

    # E4 (#111) PR-H: 暗号化証憑 (encrypted_meta_blob あり) の user 供給フィールド
    # 検証。破壊的な _delete_user_data_for_restore より前に弾く (400)。base64 本体
    # (image_data / encrypted_meta_blob / thumbnail_data) の妥当性は _b64_decode
    # (validate=True → BackupValidationError) が restore 時に保証するため、ここでは
    # int 化や DB 制約に直結する小さなフィールドを先取りで検査する。
    for row in data.get("vouchers") or []:
        if not isinstance(row, dict):
            continue  # 上の FK ループで検査済
        if row.get("encrypted_meta_blob") is None:
            continue  # 平文証憑 (レガシー) は対象外
        vid = row.get("id")
        # NG-2: export が画像/サムネ取得に失敗した E2EE 行 (_imageError /
        # _thumbnailError マーカー付き) は、復元すると暗号文の一部が欠落した
        # 復号不能な証憑になる。silent な部分復元 (200 OK) を避け 400 で弾き、
        # 利用者に完全な backup の再取得を促す。
        if "_imageError" in row or "_thumbnailError" in row:
            raise BackupValidationError(
                f"vouchers[id={vid}] は export 時に画像/サムネの取得に失敗して "
                "います (_imageError/_thumbnailError)。完全な backup を再取得して "
                "から復元してください"
            )
        # aad_id: E2EE 証憑では AAD 束縛の安定識別子として必須 (PR-G)。null だと
        # クライアントが AAD を再構築できず復号が恒久的に失敗する。値は
        # PostgreSQL BigInteger 範囲 (-2^63 .. 2^63-1) で、int() は任意精度なので
        # 範囲外や非数値を無検証で通すと DB エラー (500) になる。
        aad_raw = row.get("aad_id")
        if aad_raw is None:
            raise BackupValidationError(
                f"vouchers[id={vid}].aad_id は E2EE 証憑で必須です "
                "(AAD 束縛の安定識別子。null だとクライアント復号が不能になる)"
            )
        try:
            aad_int = int(aad_raw)
        except (TypeError, ValueError) as e:
            raise BackupValidationError(
                f"vouchers[id={vid}].aad_id が整数ではありません: {aad_raw!r}"
            ) from e
        if not (-(2 ** 63) <= aad_int < 2 ** 63):
            raise BackupValidationError(
                f"vouchers[id={vid}].aad_id が BigInteger 範囲外です: {aad_int}"
            )
        # meta_iv: AES-GCM nonce は 12 bytes 固定。encrypted_meta_blob と同じ
        # 056 で対に導入された列なので、暗号化証憑では必須。欠落 / 不正長は
        # クライアント復号が確実に失敗する (サイレント破損) ため、細工された
        # backup からの注入を復元完了扱いになる前にここで弾く。
        meta_iv_b64 = row.get("meta_iv")
        if meta_iv_b64 is None:
            raise BackupValidationError(
                f"vouchers[id={vid}].meta_iv は E2EE 証憑で必須です "
                "(encrypted_meta_blob と対で AES-GCM nonce を保持する)"
            )
        # meta_iv_b64 は上で非 None 確定なので _b64_decode は非 None を返す。
        iv = _b64_decode(meta_iv_b64)  # 不正 base64 は BackupValidationError
        if len(iv) != 12:
            raise BackupValidationError(
                f"vouchers[id={vid}].meta_iv は 12 bytes (AES-GCM nonce) "
                f"である必要があります (実際: {len(iv)})"
            )
        # file_size: 容量計上に使う非負整数。bool は int のサブクラスなので除外。
        fs = row.get("file_size")
        if fs is not None and (
            isinstance(fs, bool) or not isinstance(fs, int) or fs < 0
        ):
            raise BackupValidationError(
                f"vouchers[id={vid}].file_size は非負整数である必要があります: {fs!r}"
            )

    # journal_entries の損益振替仕訳 (fiscal_period=16 / fiscal_month=16 /
    # is_closing) は自動生成専用なので restore 経由でも注入を禁止する
    # (CLAUDE.md の複式簿記設計に従う)。E3-F で新カラムにも対応。
    for row in data.get("journal_entries") or []:
        if not isinstance(row, dict):
            raise BackupValidationError("journal_entries[*] must be an object")
        if (
            row.get("fiscal_period") == 16
            or row.get("fiscal_month") == 16
            or row.get("is_closing")
        ):
            raise BackupValidationError(
                "損益振替仕訳 (is_closing) はバックアップ復元できません"
            )

    # E3-F PR-C: encrypted_blob/blob_iv の必須化チェック。balance_cache_blobs は
    # _restore_balance_cache_blobs 側で既に検査済みなのでここでは扱わない。
    for tbl in ("journal_entries", "journal_entry_lines", "medical_expenses"):
        for row in data.get(tbl) or []:
            if not isinstance(row, dict):
                continue  # 上で既に object チェック済み (medical のみ未だが INSERT で落ちる)
            if not row.get("encrypted_blob") or not row.get("blob_iv"):
                raise BackupValidationError(
                    f"{tbl}[*].encrypted_blob/blob_iv are required "
                    "(クライアント側暗号化が必要)"
                )

    # journal_entry の貸借合計一致チェック (改ざんされた backup から不整合な
    # 仕訳を DB に書き込ませないため)
    entry_balance: dict[int, list[int]] = {}
    for row in data.get("journal_entry_lines") or []:
        eid = row.get("journal_entry_id")
        if eid is None:
            continue
        bal = entry_balance.setdefault(eid, [0, 0])
        try:
            bal[0] += int(row.get("debit_amount") or 0)
            bal[1] += int(row.get("credit_amount") or 0)
        except (TypeError, ValueError) as e:
            raise BackupValidationError(
                f"journal_entry_lines: invalid amount: {e}",
            ) from e
    for eid, (dr, cr) in entry_balance.items():
        if dr != cr:
            raise BackupValidationError(
                f"journal_entry {eid}: debit {dr} != credit {cr}"
                " (貸借不一致)",
            )


# --- delete (User と鍵類は残す) ---


def _delete_user_data_for_restore(user_id: int, backend) -> None:
    """restore 前の本人データ全削除。User row / 鍵類 / AuditGrant は保持。

    `account_deletion.delete_user_account` のサブセット。commit はしない。
    """
    # 1. VoucherAuditLog は user_id NULL 化のみ (匿名化保持)
    db.session.execute(
        update(VoucherAuditLog)
        .where(VoucherAuditLog.user_id == user_id)
        .values(user_id=None)
    )
    db.session.flush()

    # 2. Voucher: ストレージ + DB
    vouchers = Voucher.query.filter_by(user_id=user_id).all()
    for v in vouchers:
        # 平文証憑は make_thumbnail_key (_thumb.jpg)、E4 (#111) E2EE 証憑は
        # thumbnail_key (_thumb.bin) にサムネを持つ。両方を best-effort で消す
        # (取り違えても delete は冪等)。これを怠ると E2EE サムネ暗号文が
        # ストレージに孤立する。
        keys = [v.image_key, make_thumbnail_key(v.image_key)]
        if v.thumbnail_key:
            keys.append(v.thumbnail_key)
        for k in keys:
            try:
                backend.delete(k)
            except Exception as e:
                current_app.logger.warning(
                    "backup_restore: voucher storage delete failed %s: %s",
                    k, e,
                )
        db.session.delete(v)
    db.session.flush()

    # 3. AIDraft: ストレージ + DB
    drafts = AIDraft.query.filter_by(user_id=user_id).all()
    for d in drafts:
        # E5 (#111): E2EE 下書きの暗号文サムネ (thumbnail_key, _thumb.bin) も
        # 消さないと孤立する (PR-H の voucher 修正と同型)。レガシー平文下書きは
        # make_thumbnail_key (_thumb.jpg)。
        keys = []
        if d.image_key:
            keys.append(d.image_key)
            keys.append(make_thumbnail_key(d.image_key))
        if d.thumbnail_key:
            keys.append(d.thumbnail_key)
        for k in keys:
            try:
                backend.delete(k)
            except Exception as e:
                current_app.logger.warning(
                    "backup_restore: draft storage delete failed %s: %s",
                    k, e,
                )
        db.session.delete(d)
    db.session.flush()

    # 4. JournalEntryLine → JournalEntry
    entry_ids_subq = sa_select(JournalEntry.id).where(
        JournalEntry.user_id == user_id
    )
    db.session.execute(
        JournalEntryLine.__table__.delete().where(
            JournalEntryLine.journal_entry_id.in_(entry_ids_subq)
        )
    )
    db.session.flush()
    JournalEntry.query.filter_by(user_id=user_id).delete()
    db.session.flush()

    # 5. その他 user_id を持つテーブル (鍵類 / AuditGrant / 使用量履歴は除く)
    for model in (
        UserAIConfig,
        BalanceCacheBlob,
        CsvColumnProfile,
        FiscalClose,
        MedicalExpense,
    ):
        model.query.filter_by(user_id=user_id).delete()

    # TaxFormMapping (user_id は ForeignKey なし)
    db.session.execute(
        TaxFormMapping.__table__.delete().where(
            TaxFormMapping.user_id == user_id
        )
    )

    # Account は最後 (FK 依存) — JournalEntryLine.account_code / TaxFormMapping
    # 等の参照を全部消した後でないと外せない
    Account.query.filter_by(user_id=user_id).delete()
    db.session.flush()


# --- helpers ---


def _b64_decode(s: Any) -> bytes | None:
    if s is None:
        return None
    if not isinstance(s, str):
        raise BackupValidationError("image_data must be a base64 string or null")
    try:
        return base64.b64decode(s, validate=True)
    except Exception as e:
        raise BackupValidationError(f"invalid base64: {e}") from e


def _parse_date(s: Any) -> date | None:
    if s is None:
        return None
    if isinstance(s, date):
        return s
    if not isinstance(s, str):
        raise BackupValidationError(f"date must be ISO string or null: {s!r}")
    try:
        return date.fromisoformat(s)
    except Exception as e:
        raise BackupValidationError(f"invalid date: {s!r}: {e}") from e


def _parse_datetime(s: Any) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _cleanup_storage(backend, keys: list[str]) -> None:
    for k in keys:
        try:
            backend.delete(k)
        except Exception as e:
            current_app.logger.warning(
                "backup_restore: cleanup storage delete failed %s: %s", k, e,
            )


# --- restore-by-table helpers ---


def _restore_accounts(user_id: int, rows: list[dict]) -> int:
    for r in rows:
        db.session.add(Account(
            user_id=user_id,
            code=r.get("code"),
            account_type_id=r.get("account_type_id"),
            name=r.get("name", ""),
            description=r.get("description", "") or "",
            tax_category=r.get("tax_category"),
            cost_type=r.get("cost_type"),
            system_role=r.get("system_role"),
            is_system=bool(r.get("is_system", False)),
            is_active=bool(r.get("is_active", True)),
            deactivated_year=r.get("deactivated_year"),
            display_order=r.get("display_order", 0) or 0,
        ))
    return len(rows)


def _restore_fiscal_closes(user_id: int, rows: list[dict]) -> int:
    for r in rows:
        db.session.add(FiscalClose(
            user_id=user_id,
            year=r.get("year"),
            closed_period=r.get("closed_period", -1),
        ))
    return len(rows)


def _restore_journal_entries(
    user_id: int, rows: list[dict],
) -> dict[int, int]:
    """{old_id: new_id} のマップを返す。"""
    id_map: dict[int, int] = {}
    for r in rows:
        old_id = r.get("id")
        entry_date = _parse_date(r.get("date"))
        fiscal_period = r.get("fiscal_period")
        # E3-F: is_closing / fiscal_month は新フォーマットならそのまま、
        # 旧フォーマット (これらキー無し) なら source / fiscal_period / date
        # から導出する (forward-compat)。
        is_closing = r.get("is_closing")
        if is_closing is None:
            is_closing = (r.get("source") == "closing")
        fiscal_month = r.get("fiscal_month")
        if fiscal_month is None:
            if fiscal_period is not None:
                fiscal_month = fiscal_period
            elif entry_date is not None:
                fiscal_month = entry_date.month
        # E3-F PR-D-6-4: 平文 date / description / source / fiscal_period 列は
        # 書き込まない (本体は encrypted_blob のみ)。entry_date / fiscal_period /
        # source は旧フォーマット backup の forward-compat で fiscal_month /
        # is_closing を導出するためにのみ読む。
        entry = JournalEntry(
            user_id=user_id,
            entry_number=r.get("entry_number"),
            batch_id=r.get("batch_id"),
            fiscal_year=r.get("fiscal_year"),
            is_closing=bool(is_closing),
            fiscal_month=fiscal_month,
            encrypted_blob=_b64_decode(r.get("encrypted_blob")),
            blob_iv=_b64_decode(r.get("blob_iv")),
        )
        db.session.add(entry)
        db.session.flush()  # PK を確定させて id_map に積む
        if old_id is not None:
            id_map[old_id] = entry.id
    return id_map


def _restore_journal_entry_lines(
    user_id: int, rows: list[dict], entry_id_map: dict[int, int],
) -> int:
    n = 0
    for r in rows:
        old_eid = r.get("journal_entry_id")
        new_eid = entry_id_map.get(old_eid)
        if new_eid is None:
            # 上の _validate_backup で防いでいるが念のため
            raise BackupValidationError(
                f"journal_entry_lines: unmapped journal_entry_id {old_eid}",
            )
        # #338 item5: 平文 account_code / debit / credit / description は書き込まない
        # (NULL)。line 本体は encrypted_blob のみ。backup にこれらが残っていても無視する。
        db.session.add(JournalEntryLine(
            journal_entry_id=new_eid,
            account_user_id=user_id,
            account_code=None,
            debit_amount=None,
            credit_amount=None,
            encrypted_blob=_b64_decode(r.get("encrypted_blob")),
            blob_iv=_b64_decode(r.get("blob_iv")),
        ))
        n += 1
    return n


def _restore_medical_expenses(
    user_id: int, rows: list[dict], entry_id_map: dict[int, int],
) -> int:
    for r in rows:
        old_eid = r.get("journal_entry_id")
        new_eid = entry_id_map.get(old_eid) if old_eid is not None else None
        # E3-F PR-D-6-4: 平文列 (date / patient_name / hospital_name /
        # treatment_description / provider_type / amount_paid /
        # insurance_reimbursement) は書き込まない (本体は encrypted_blob のみ)。
        db.session.add(MedicalExpense(
            user_id=user_id,
            journal_entry_id=new_eid,
            encrypted_blob=_b64_decode(r.get("encrypted_blob")),
            blob_iv=_b64_decode(r.get("blob_iv")),
        ))
    return len(rows)


def _restore_balance_cache_blobs(user_id: int, rows: list[dict]) -> int:
    for r in rows:
        blob = _b64_decode(r.get("encrypted_blob"))
        iv = _b64_decode(r.get("blob_iv"))
        if blob is None or iv is None:
            raise BackupValidationError(
                "balance_cache_blobs[*].encrypted_blob/blob_iv are required",
            )
        db.session.add(BalanceCacheBlob(
            user_id=user_id,
            year=r["year"],
            period=r["period"],
            encrypted_blob=blob,
            blob_iv=iv,
        ))
    return len(rows)


def _restore_vouchers(
    user_id: int, rows: list[dict],
    entry_id_map: dict[int, int],
    backend, written_keys: list[str],
) -> tuple[int, int]:
    """Returns (db_count, storage_count)."""
    db_n = 0
    storage_n = 0
    for r in rows:
        image_data_b64 = r.get("image_data")
        if image_data_b64 is None:
            # export 時に取得失敗した行 (_imageError 持ち) は skip
            current_app.logger.info(
                "backup_restore: voucher skipped (no image_data)",
            )
            continue
        image_bytes = _b64_decode(image_data_b64)
        old_eid = r.get("journal_entry_id")
        new_eid = entry_id_map.get(old_eid) if old_eid is not None else None
        # 改ざん検知: 画像バイトから SHA-256 を再計算して file_hash に採用
        # (backup の file_hash と画像バイトが不一致のまま保存しない)。E2EE 証憑では
        # image_bytes は暗号文 (iv||ct||tag) なので、これは finalize_voucher_upload
        # が保存する cipher hash と一致する (電帳法 Q11 ハイブリッドの cipher 側)。
        computed_hash = hashlib.sha256(image_bytes).hexdigest()

        meta_blob_b64 = r.get("encrypted_meta_blob")
        if meta_blob_b64 is not None:
            # --- E4 (#111) E2EE 証憑: 暗号文をそのまま保存 (Pillow を回さない) ---
            # サーバは復号できないので画像/サムネ暗号文を無加工で書き戻し、復号に
            # 必要な暗号メタ列 (encrypted_meta_blob / meta_iv / file_hash_plain /
            # aad_id) を復元する。aad_id を往復保持することで、PK 再採番後も AAD が
            # 一致しクライアント復号が成立する (Option C / PR-G の基盤)。
            thumb_b64 = r.get("thumbnail_data")
            thumb_bytes = _b64_decode(thumb_b64) if thumb_b64 is not None else None
            aad_raw = r.get("aad_id")
            meta_iv_b64 = r.get("meta_iv")
            backed_size = r.get("file_size")
            voucher = Voucher(
                user_id=user_id,
                journal_entry_id=new_eid,
                image_key="",  # 仮、後で setattr
                # E5 PR-5 (#111): image_mime 列は DROP 済 (実 MIME は blob 内)。
                file_hash=computed_hash,  # cipher hash
                file_hash_plain=r.get("file_hash_plain"),
                encrypted_meta_blob=_b64_decode(meta_blob_b64),
                meta_iv=_b64_decode(meta_iv_b64) if meta_iv_b64 else None,
                aad_id=int(aad_raw) if aad_raw is not None else None,
                # E2EE の file_size は画像+サムネ暗号文の合計 (finalize と同じ)。
                # backup 値を優先し、欠落時のみ再計算する。
                file_size=backed_size if backed_size is not None
                else len(image_bytes) + (len(thumb_bytes) if thumb_bytes else 0),
                uploaded_at=_parse_datetime(r.get("uploaded_at")) or datetime.now(timezone.utc),
            )
            db.session.add(voucher)
            db.session.flush()
            new_key = make_storage_key(user_id, voucher.id, ENCRYPTED_CONTENT_TYPE)
            voucher.image_key = new_key
            # WARN-5: put が途中で失敗 (S3 partial write 等) しても
            # _cleanup_storage が孤立オブジェクトを消せるよう、append を put より
            # 先に行う。best-effort delete なので未書込キーでも無害。
            written_keys.append(new_key)
            backend.put(new_key, image_bytes, ENCRYPTED_CONTENT_TYPE)
            if thumb_bytes is not None:
                thumb_key = make_encrypted_thumbnail_key(new_key)
                voucher.thumbnail_key = thumb_key
                written_keys.append(thumb_key)
                backend.put(thumb_key, thumb_bytes, ENCRYPTED_CONTENT_TYPE)
            db_n += 1
            storage_n += 1
            continue

        # --- レガシー平文証憑 (AI クイックアクセプト等): 従来通り Pillow サムネ生成 ---
        # E5 PR-5 (#111): image_mime 列は DROP 済。storage key の拡張子と Pillow
        # サムネ生成には backup JSON の mime を使う (列ではなくローカル変数)。
        # Pillow は実バイトから形式を判定するため値が不正確でも実害は小さい。
        plain_mime = r.get("image_mime") or "application/octet-stream"
        voucher = Voucher(
            user_id=user_id,
            journal_entry_id=new_eid,
            image_key="",  # 仮、後で setattr
            file_hash=computed_hash,
            file_size=len(image_bytes),
            uploaded_at=_parse_datetime(r.get("uploaded_at")) or datetime.now(timezone.utc),
        )
        db.session.add(voucher)
        db.session.flush()
        # 新 PK で storage key を採番し直す
        new_key = make_storage_key(user_id, voucher.id, plain_mime)
        voucher.image_key = new_key
        store_image_with_thumbnail(new_key, image_bytes, plain_mime)
        written_keys.append(new_key)
        written_keys.append(make_thumbnail_key(new_key))
        db_n += 1
        storage_n += 1
    return db_n, storage_n


def _restore_ai_drafts(
    user_id: int, rows: list[dict],
    backend, written_keys: list[str],
) -> tuple[int, int]:
    db_n = 0
    storage_n = 0
    for r in rows:
        image_data_b64 = r.get("image_data")
        if image_data_b64 is None:
            current_app.logger.info(
                "backup_restore: ai_draft skipped (no image_data)",
            )
            continue
        image_bytes = _b64_decode(image_data_b64)
        computed_hash = hashlib.sha256(image_bytes).hexdigest()
        draft = AIDraft(
            user_id=user_id,
            image_key="",  # 後で setattr
            image_mime=r.get("image_mime", "application/octet-stream"),
            file_hash=computed_hash,
            file_size=len(image_bytes),
            comment=r.get("comment", "") or "",
            suggestions_json=r.get("suggestions_json"),
            status=r.get("status", "pending") or "pending",
        )
        db.session.add(draft)
        db.session.flush()
        new_key = make_storage_key(user_id, draft.id, draft.image_mime)
        draft.image_key = new_key
        store_image_with_thumbnail(new_key, image_bytes, draft.image_mime)
        written_keys.append(new_key)
        written_keys.append(make_thumbnail_key(new_key))
        db_n += 1
        storage_n += 1
    return db_n, storage_n


def _restore_user_ai_config(user_id: int, cfg: dict | None) -> int:
    if cfg is None:
        return 0
    db.session.add(UserAIConfig(
        user_id=user_id,
        provider=cfg.get("provider", "openai") or "openai",
        api_key_blob=_b64_decode(cfg.get("api_key_blob")),
        api_key_iv=_b64_decode(cfg.get("api_key_iv")),
        model_name=cfg.get("model_name", "") or "",
        custom_prompt=cfg.get("custom_prompt", "") or "",
        compliance_check=bool(cfg.get("compliance_check", False)),
    ))
    return 1


def _restore_tax_form_mappings(user_id: int, rows: list[dict]) -> int:
    for r in rows:
        db.session.add(TaxFormMapping(
            user_id=user_id,
            account_code=r.get("account_code"),
            field_id=r.get("field_id"),
        ))
    return len(rows)


def _restore_csv_column_profiles(user_id: int, rows: list[dict]) -> int:
    for r in rows:
        db.session.add(CsvColumnProfile(
            user_id=user_id,
            account_code=r.get("account_code"),
            date_col=r.get("date_col"),
            desc_col=r.get("desc_col"),
            deposit_col=r.get("deposit_col"),
            withdrawal_col=r.get("withdrawal_col"),
            amount_col=r.get("amount_col"),
            date_format=r.get("date_format", "%Y/%m/%d") or "%Y/%m/%d",
            amount_mode=r.get("amount_mode", "separate") or "separate",
        ))
    return len(rows)


# --- public entry point ---


def restore_user_backup(user_id: int, backup: Any) -> dict:
    """全置換 restore のエントリポイント。

    Returns:
        {"tables": {<table>: <count>}, "storage": {"vouchers": N, "ai_drafts": N}}
    Raises:
        BackupValidationError: HTTP 400 相当
        BackupRestoreError:    HTTP 500 相当 (rollback 後に re-raise)
    """
    _validate_backup(user_id, backup)
    backend = get_storage_backend()
    written_keys: list[str] = []
    try:
        _delete_user_data_for_restore(user_id, backend)
        data = backup["data"]
        counts: dict[str, int] = {}
        counts["accounts"] = _restore_accounts(user_id, data.get("accounts") or [])
        db.session.flush()
        counts["fiscal_closes"] = _restore_fiscal_closes(
            user_id, data.get("fiscal_closes") or [],
        )
        entry_id_map = _restore_journal_entries(
            user_id, data.get("journal_entries") or [],
        )
        counts["journal_entries"] = len(entry_id_map)
        counts["journal_entry_lines"] = _restore_journal_entry_lines(
            user_id, data.get("journal_entry_lines") or [], entry_id_map,
        )
        counts["medical_expenses"] = _restore_medical_expenses(
            user_id, data.get("medical_expenses") or [], entry_id_map,
        )
        counts["balance_cache_blobs"] = _restore_balance_cache_blobs(
            user_id, data.get("balance_cache_blobs") or [],
        )
        db.session.flush()
        v_db, v_st = _restore_vouchers(
            user_id, data.get("vouchers") or [], entry_id_map,
            backend, written_keys,
        )
        counts["vouchers"] = v_db
        d_db, d_st = _restore_ai_drafts(
            user_id, data.get("ai_drafts") or [], backend, written_keys,
        )
        counts["ai_drafts"] = d_db
        counts["user_ai_config"] = _restore_user_ai_config(
            user_id, data.get("user_ai_config"),
        )
        counts["tax_form_mappings"] = _restore_tax_form_mappings(
            user_id, data.get("tax_form_mappings") or [],
        )
        counts["csv_column_profiles"] = _restore_csv_column_profiles(
            user_id, data.get("csv_column_profiles") or [],
        )
        db.session.commit()
        return {
            "tables": counts,
            "storage": {"vouchers": v_st, "ai_drafts": d_st},
        }
    except BackupValidationError:
        db.session.rollback()
        _cleanup_storage(backend, written_keys)
        raise
    except Exception as e:
        db.session.rollback()
        _cleanup_storage(backend, written_keys)
        current_app.logger.exception(
            "restore_user_backup failed for user %s", user_id,
        )
        raise BackupRestoreError(str(e)) from e
