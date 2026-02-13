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


def create_cashbook_entry(user_id, date, transaction_type, payment_account_id,
                          category_account_id, amount, description):
    """出納帳の入力から仕訳を自動生成する

    Args:
        transaction_type: 'income' or 'expense'
        payment_account_id: 支払元/入金先の勘定科目ID（資産・負債）
        category_account_id: 費目の勘定科目ID（収益・費用）
        amount: 金額（正の整数）
    """
    entry = JournalEntry(
        user_id=user_id,
        date=date,
        entry_number=get_next_entry_number(user_id),
        description=description,
        source="cashbook",
    )
    db.session.add(entry)
    db.session.flush()

    if transaction_type == "expense":
        # 支出: 借方=費用科目 / 貸方=資産（or負債）科目
        lines = [
            JournalEntryLine(
                journal_entry_id=entry.id,
                account_id=category_account_id,
                debit_amount=amount,
                credit_amount=0,
            ),
            JournalEntryLine(
                journal_entry_id=entry.id,
                account_id=payment_account_id,
                debit_amount=0,
                credit_amount=amount,
            ),
        ]
    else:
        # 収入: 借方=資産科目 / 貸方=収益科目
        lines = [
            JournalEntryLine(
                journal_entry_id=entry.id,
                account_id=payment_account_id,
                debit_amount=amount,
                credit_amount=0,
            ),
            JournalEntryLine(
                journal_entry_id=entry.id,
                account_id=category_account_id,
                debit_amount=0,
                credit_amount=amount,
            ),
        ]

    db.session.add_all(lines)
    db.session.commit()
    return entry


def create_journal_entry(user_id, date, description, lines_data):
    """仕訳伝票を直接作成する

    Args:
        lines_data: list of dict with keys: account_id, debit_amount, credit_amount
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
        source="journal",
    )
    db.session.add(entry)
    db.session.flush()

    for line_data in lines_data:
        line = JournalEntryLine(
            journal_entry_id=entry.id,
            account_id=line_data["account_id"],
            debit_amount=line_data["debit_amount"],
            credit_amount=line_data["credit_amount"],
            description=line_data.get("description", ""),
        )
        db.session.add(line)

    db.session.commit()
    return entry


def update_cashbook_entry(entry, date, transaction_type, payment_account_id,
                          category_account_id, amount, description):
    """出納帳の仕訳を更新"""
    entry.date = date
    entry.description = description

    # 既存の明細を削除して再作成
    for line in entry.lines:
        db.session.delete(line)
    db.session.flush()

    if transaction_type == "expense":
        lines = [
            JournalEntryLine(
                journal_entry_id=entry.id,
                account_id=category_account_id,
                debit_amount=amount,
                credit_amount=0,
            ),
            JournalEntryLine(
                journal_entry_id=entry.id,
                account_id=payment_account_id,
                debit_amount=0,
                credit_amount=amount,
            ),
        ]
    else:
        lines = [
            JournalEntryLine(
                journal_entry_id=entry.id,
                account_id=payment_account_id,
                debit_amount=amount,
                credit_amount=0,
            ),
            JournalEntryLine(
                journal_entry_id=entry.id,
                account_id=category_account_id,
                debit_amount=0,
                credit_amount=amount,
            ),
        ]

    db.session.add_all(lines)
    db.session.commit()
    return entry
