"""仕訳自動生成サービス"""

from app.extensions import db
from app.models.journal import JournalEntry, JournalEntryLine


def get_next_entry_number(user_id):
    """次の伝票番号を取得"""
    max_num = (
        db.session.query(db.func.max(JournalEntry.entry_number))
        .filter(JournalEntry.user_id == user_id)
        .scalar()
    )
    return (max_num or 0) + 1


def create_journal_entry(user_id, date, description, lines_data,
                         source="journal", batch_id=None, fiscal_period=None,
                         *, encrypted_blob=None, blob_iv=None,
                         fiscal_year=None, commit=True):
    """仕訳伝票を直接作成する

    Args:
        lines_data: list of dict with keys: account_code, debit_amount,
            credit_amount, description, optional encrypted_blob/blob_iv
            (Phase E3: クライアント側で AES-GCM 暗号化済の line 本体)
        source: 仕訳の入力元（"journal", "ai_receipt" 等）
        fiscal_period: 計上期間（None=日付の月で自動判定）
        encrypted_blob/blob_iv: Phase E3 - クライアント側で AES-GCM 暗号化された
            entry 本体 (date / description / source / batch_id / fiscal_period の
            暗号化版)。両方セット or 両方 None。
        fiscal_year: Phase E3 - 平文の年度フィルタ用 (date 暗号化後の代替)。
            None なら date.year を使用。
        commit: False を指定するとセッションを commit せず flush のみ行う。
            複数 entry をまとめて 1 トランザクションにする batch API 用。
    """
    total_debit = sum(l["debit_amount"] for l in lines_data)
    total_credit = sum(l["credit_amount"] for l in lines_data)
    if total_debit != total_credit:
        raise ValueError(
            f"貸借が一致しません（借方: {total_debit}, 貸方: {total_credit}）"
        )

    if (encrypted_blob is None) != (blob_iv is None):
        raise ValueError("encrypted_blob と blob_iv は同時に指定が必要です。")
    # 多層防御: API 以外の caller が短い IV で保存しないよう service 層でも検査。
    if blob_iv is not None and len(blob_iv) != 12:
        raise ValueError(
            "blob_iv は 12B (AES-GCM IV) である必要があります。",
        )

    entry = JournalEntry(
        user_id=user_id,
        date=date,
        entry_number=get_next_entry_number(user_id),
        description=description,
        source=source,
        batch_id=batch_id,
        fiscal_period=fiscal_period,
        encrypted_blob=encrypted_blob,
        blob_iv=blob_iv,
        fiscal_year=fiscal_year if fiscal_year is not None else date.year,
        # E3-F: 平文 fiscal_period / date と並行して新カラムを populate。
        fiscal_month=fiscal_period if fiscal_period is not None else date.month,
    )
    db.session.add(entry)
    db.session.flush()

    for line_data in lines_data:
        line_blob = line_data.get("encrypted_blob")
        line_iv = line_data.get("blob_iv")
        if (line_blob is None) != (line_iv is None):
            raise ValueError(
                "line の encrypted_blob と blob_iv は同時に指定が必要です。",
            )
        if line_iv is not None and len(line_iv) != 12:
            raise ValueError(
                "line の blob_iv は 12B (AES-GCM IV) である必要があります。",
            )
        line = JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=line_data["account_code"],
            debit_amount=line_data["debit_amount"],
            credit_amount=line_data["credit_amount"],
            description=line_data.get("description", ""),
            encrypted_blob=line_blob,
            blob_iv=line_iv,
        )
        db.session.add(line)

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return entry
