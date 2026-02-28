"""CSV明細照合（マッチング）サービス"""

from collections import defaultdict
from datetime import date, timedelta

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine

MATCH_DATE_TOLERANCE = 5  # 日付許容範囲（±日）


def find_matches(user_id, payment_account_id, csv_rows):
    """CSV parsed行と既存仕訳のマッチングを行う。

    Args:
        user_id: ユーザーID
        payment_account_id: 支払元口座ID
        csv_rows: list of dict — parse_csv_full() の戻り値と同じ形式
            date: str (ISO format) or None
            description: str
            deposit: int
            withdrawal: int

    Returns:
        list of dict — csv_rows と同じ順序。各要素:
            csv_index: int
            status: "matched" | "multiple" | "unmatched"
            matches: list of dict
                entry_id, date, description, amount, source, category_name
    """
    if not csv_rows:
        return []

    # Phase 1: CSV行から日付範囲を算出
    all_dates = []
    for row in csv_rows:
        d = row.get("date")
        if d:
            if isinstance(d, str):
                try:
                    all_dates.append(date.fromisoformat(d))
                except ValueError:
                    pass
            elif isinstance(d, date):
                all_dates.append(d)

    if not all_dates:
        return [
            {"csv_index": i, "status": "unmatched", "matches": []}
            for i in range(len(csv_rows))
        ]

    date_min = min(all_dates) - timedelta(days=MATCH_DATE_TOLERANCE)
    date_max = max(all_dates) + timedelta(days=MATCH_DATE_TOLERANCE)

    # Phase 2: 支払元口座の仕訳を一括取得
    candidates = (
        db.session.query(
            JournalEntry.id.label("entry_id"),
            JournalEntry.date,
            JournalEntry.description,
            JournalEntry.source,
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            JournalEntry.user_id == user_id,
            JournalEntryLine.account_id == payment_account_id,
            JournalEntry.date >= date_min,
            JournalEntry.date <= date_max,
        )
        .all()
    )

    # Phase 3: 相手科目名をバッチ取得
    entry_ids = list({c.entry_id for c in candidates})
    counterpart_map = {}
    if entry_ids:
        counterparts = (
            db.session.query(
                JournalEntryLine.journal_entry_id,
                Account.name,
            )
            .join(Account, Account.id == JournalEntryLine.account_id)
            .filter(
                JournalEntryLine.journal_entry_id.in_(entry_ids),
                JournalEntryLine.account_id != payment_account_id,
            )
            .all()
        )
        for cp in counterparts:
            counterpart_map.setdefault(cp.journal_entry_id, []).append(cp.name)

    # Phase 4: 金額インデックス構築
    amount_index = defaultdict(list)
    for c in candidates:
        credit = int(c.credit_amount) if c.credit_amount else 0
        debit = int(c.debit_amount) if c.debit_amount else 0
        if credit > 0:
            amount_index[(credit, "withdrawal")].append(c)
        if debit > 0:
            amount_index[(debit, "deposit")].append(c)

    # Phase 5: 各CSV行をマッチング
    used_entry_ids = set()
    results = []

    for i, row in enumerate(csv_rows):
        raw_date = row.get("date")
        if not raw_date:
            results.append({"csv_index": i, "status": "unmatched", "matches": []})
            continue

        if isinstance(raw_date, str):
            try:
                csv_date = date.fromisoformat(raw_date)
            except ValueError:
                results.append({"csv_index": i, "status": "unmatched", "matches": []})
                continue
        else:
            csv_date = raw_date

        withdrawal = int(row.get("withdrawal") or 0)
        deposit = int(row.get("deposit") or 0)

        if withdrawal > 0:
            amount, direction = withdrawal, "withdrawal"
        elif deposit > 0:
            amount, direction = deposit, "deposit"
        else:
            results.append({"csv_index": i, "status": "unmatched", "matches": []})
            continue

        potential = amount_index.get((amount, direction), [])
        matched = []
        for c in potential:
            if c.entry_id in used_entry_ids:
                continue
            if abs((c.date - csv_date).days) <= MATCH_DATE_TOLERANCE:
                cat_names = counterpart_map.get(c.entry_id, [])
                matched.append({
                    "entry_id": c.entry_id,
                    "date": c.date.isoformat(),
                    "description": c.description,
                    "amount": amount,
                    "source": c.source,
                    "category_name": ", ".join(cat_names),
                })

        # 日付が近い順にソート
        matched.sort(key=lambda m: abs((date.fromisoformat(m["date"]) - csv_date).days))

        if len(matched) == 1:
            used_entry_ids.add(matched[0]["entry_id"])
            results.append({"csv_index": i, "status": "matched", "matches": matched})
        elif len(matched) > 1:
            results.append({"csv_index": i, "status": "multiple", "matches": matched})
        else:
            results.append({"csv_index": i, "status": "unmatched", "matches": []})

    return results
