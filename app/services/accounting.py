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


def create_cashbook_entry(user_id, date, transaction_type, payment_account_code,
                          category_account_code, amount, description,
                          batch_id=None, fiscal_period=None, source="cashbook"):
    """出納帳の入力から仕訳を自動生成する

    Args:
        transaction_type: 'income' or 'expense'
        payment_account_code: 支払元/入金先の勘定科目コード（資産・負債）
        category_account_code: 費目の勘定科目コード（収益・費用）
        amount: 金額（正の整数）
        fiscal_period: 計上期間（None=日付の月で自動判定）
    """
    entry = JournalEntry(
        user_id=user_id,
        date=date,
        entry_number=get_next_entry_number(user_id),
        description=description,
        source=source,
        batch_id=batch_id,
        fiscal_period=fiscal_period,
    )
    db.session.add(entry)
    db.session.flush()

    abs_amount = abs(amount)
    if transaction_type == "expense":
        debit_code, credit_code = category_account_code, payment_account_code
    else:
        debit_code, credit_code = payment_account_code, category_account_code
    if amount < 0:
        debit_code, credit_code = credit_code, debit_code

    lines = [
        JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=debit_code,
            debit_amount=abs_amount,
            credit_amount=0,
        ),
        JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=credit_code,
            debit_amount=0,
            credit_amount=abs_amount,
        ),
    ]

    db.session.add_all(lines)
    db.session.commit()
    return entry


def create_journal_entry(user_id, date, description, lines_data,
                         source="journal", batch_id=None, fiscal_period=None):
    """仕訳伝票を直接作成する

    Args:
        lines_data: list of dict with keys: account_code, debit_amount, credit_amount
        source: 仕訳の入力元（"journal", "ai_receipt" 等）
        fiscal_period: 計上期間（None=日付の月で自動判定）
    """
    total_debit = sum(l["debit_amount"] for l in lines_data)
    total_credit = sum(l["credit_amount"] for l in lines_data)
    if total_debit != total_credit:
        raise ValueError(
            f"貸借が一致しません（借方: {total_debit}, 貸方: {total_credit}）"
        )

    entry = JournalEntry(
        user_id=user_id,
        date=date,
        entry_number=get_next_entry_number(user_id),
        description=description,
        source=source,
        batch_id=batch_id,
        fiscal_period=fiscal_period,
    )
    db.session.add(entry)
    db.session.flush()

    for line_data in lines_data:
        line = JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=line_data["account_code"],
            debit_amount=line_data["debit_amount"],
            credit_amount=line_data["credit_amount"],
            description=line_data.get("description", ""),
        )
        db.session.add(line)

    db.session.commit()
    return entry


def create_transfer_entry(user_id, date, from_account_code, to_account_code,
                          amount, description, batch_id=None, fiscal_period=None,
                          source="cashbook"):
    """口座間振替の仕訳を作成する

    Args:
        from_account_code: 出金元の勘定科目コード（貸方）
        to_account_code: 入金先の勘定科目コード（借方）
        amount: 金額（正の整数）
        fiscal_period: 計上期間（None=日付の月で自動判定）
    """
    abs_amount = abs(amount)
    debit_code, credit_code = to_account_code, from_account_code
    if amount < 0:
        debit_code, credit_code = credit_code, debit_code

    entry = JournalEntry(
        user_id=user_id,
        date=date,
        entry_number=get_next_entry_number(user_id),
        description=description,
        source=source,
        batch_id=batch_id,
        fiscal_period=fiscal_period,
    )
    db.session.add(entry)
    db.session.flush()

    lines = [
        JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=debit_code,
            debit_amount=abs_amount,
            credit_amount=0,
        ),
        JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=credit_code,
            debit_amount=0,
            credit_amount=abs_amount,
        ),
    ]
    db.session.add_all(lines)
    db.session.commit()
    return entry


def update_transfer_entry(entry, date, from_account_code, to_account_code,
                          amount, description):
    """口座間振替の仕訳を更新する"""
    entry.date = date
    entry.description = description

    for line in entry.lines:
        db.session.delete(line)
    db.session.flush()

    user_id = entry.user_id
    abs_amount = abs(amount)
    debit_code, credit_code = to_account_code, from_account_code
    if amount < 0:
        debit_code, credit_code = credit_code, debit_code

    lines = [
        JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=debit_code,
            debit_amount=abs_amount,
            credit_amount=0,
        ),
        JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=credit_code,
            debit_amount=0,
            credit_amount=abs_amount,
        ),
    ]
    db.session.add_all(lines)
    db.session.commit()
    return entry


def update_cashbook_entry(entry, date, transaction_type, payment_account_code,
                          category_account_code, amount, description):
    """出納帳の仕訳を更新"""
    entry.date = date
    entry.description = description

    # 既存の明細を削除して再作成
    for line in entry.lines:
        db.session.delete(line)
    db.session.flush()

    user_id = entry.user_id
    abs_amount = abs(amount)

    if transaction_type == "expense":
        debit_code, credit_code = category_account_code, payment_account_code
    else:
        debit_code, credit_code = payment_account_code, category_account_code
    if amount < 0:
        debit_code, credit_code = credit_code, debit_code

    lines = [
        JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=debit_code,
            debit_amount=abs_amount,
            credit_amount=0,
        ),
        JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=credit_code,
            debit_amount=0,
            credit_amount=abs_amount,
        ),
    ]

    db.session.add_all(lines)
    db.session.commit()
    return entry
