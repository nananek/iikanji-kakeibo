"""CSV明細照合（マッチング）サービス"""

from collections import defaultdict
from datetime import date, timedelta

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine

MATCH_DATE_TOLERANCE = 0  # 日付完全一致のみ


def find_matches(user_id, payment_account_code, csv_rows):
    """CSV parsed行と既存仕訳のマッチングを行う。

    Args:
        user_id: ユーザーID
        payment_account_code: 支払元口座コード
        csv_rows: list of dict — parse_csv_full() の戻り値と同じ形式
            date: str (ISO format) or None
            description: str
            deposit: int
            withdrawal: int

    Returns:
        dict:
            csv_results: list of dict — csv_rows と同じ順序
                csv_index, status, matches
            journal_only: list of dict — CSVにマッチしなかった仕訳
                entry_id, date, description, amount, direction, source, category_name
            daily_summary: list of dict — 日付ごとの比較サマリー
                date, csv_count, csv_total, journal_count, journal_total,
                diff_amount, has_discrepancy
    """
    if not csv_rows:
        return {"csv_results": [], "journal_only": [], "daily_summary": []}

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
        csv_results = [
            {"csv_index": i, "status": "unmatched", "matches": []}
            for i in range(len(csv_rows))
        ]
        return {
            "csv_results": csv_results,
            "journal_only": [],
            "daily_summary": [],
        }

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
            JournalEntryLine.account_user_id == user_id,
            JournalEntryLine.account_code == payment_account_code,
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
            .join(Account, db.and_(
                Account.user_id == JournalEntryLine.account_user_id,
                Account.code == JournalEntryLine.account_code,
            ))
            .filter(
                JournalEntryLine.journal_entry_id.in_(entry_ids),
                JournalEntryLine.account_code != payment_account_code,
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

    # Phase 6: CSV にマッチしなかった仕訳を検出 (journal_only)
    referenced_ids = set()
    for r in results:
        for m in r["matches"]:
            referenced_ids.add(m["entry_id"])

    journal_only = []
    for c in candidates:
        if c.entry_id in referenced_ids:
            continue
        credit = int(c.credit_amount) if c.credit_amount else 0
        debit = int(c.debit_amount) if c.debit_amount else 0
        if credit > 0:
            amount, direction = credit, "withdrawal"
        elif debit > 0:
            amount, direction = debit, "deposit"
        else:
            continue
        cat_names = counterpart_map.get(c.entry_id, [])
        journal_only.append({
            "entry_id": c.entry_id,
            "date": c.date.isoformat(),
            "description": c.description,
            "amount": amount,
            "direction": direction,
            "source": c.source,
            "category_name": ", ".join(cat_names),
        })

    # Phase 7: 日計サマリーを構築
    daily_summary = _build_daily_summary(csv_rows, results, candidates,
                                         counterpart_map, used_entry_ids)

    return {
        "csv_results": results,
        "journal_only": journal_only,
        "daily_summary": daily_summary,
    }


def _build_daily_summary(csv_rows, csv_results, candidates, counterpart_map,
                         used_entry_ids):
    """日付ごとのCSV vs 仕訳の比較サマリーを生成する。"""
    # CSV側: 日付ごとの件数・合計
    csv_by_date = defaultdict(lambda: {"count": 0, "total": 0})
    for row in csv_rows:
        d = row.get("date")
        if not d:
            continue
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except ValueError:
                continue
        amount = int(row.get("withdrawal") or 0) + int(row.get("deposit") or 0)
        csv_by_date[d]["count"] += 1
        csv_by_date[d]["total"] += amount

    # 仕訳側: 日付ごとの件数・合計（支払元口座の行のみ）
    journal_by_date = defaultdict(lambda: {"count": 0, "total": 0})
    seen_entries = set()
    for c in candidates:
        if c.entry_id in seen_entries:
            continue
        seen_entries.add(c.entry_id)
        credit = int(c.credit_amount) if c.credit_amount else 0
        debit = int(c.debit_amount) if c.debit_amount else 0
        amount = credit + debit
        journal_by_date[c.date]["count"] += 1
        journal_by_date[c.date]["total"] += amount

    # 全日付を統合してサマリー生成
    all_dates = sorted(set(csv_by_date.keys()) | set(journal_by_date.keys()))
    summary = []
    for d in all_dates:
        csv_info = csv_by_date.get(d, {"count": 0, "total": 0})
        jnl_info = journal_by_date.get(d, {"count": 0, "total": 0})
        diff_amount = csv_info["total"] - jnl_info["total"]
        diff_count = csv_info["count"] - jnl_info["count"]
        summary.append({
            "date": d.isoformat(),
            "csv_count": csv_info["count"],
            "csv_total": csv_info["total"],
            "journal_count": jnl_info["count"],
            "journal_total": jnl_info["total"],
            "diff_count": diff_count,
            "diff_amount": diff_amount,
            "has_discrepancy": diff_amount != 0 or diff_count != 0,
        })

    return summary


# --- AI 照合 ---

AI_RECONCILE_PROMPT = """\
あなたは日本の家計簿アプリの照合アシスタントです。
以下はクレジットカード等のCSV明細と、既存の仕訳一覧です。
金額が完全一致しないものの、同一取引である可能性があるペアを見つけてください。

照合のヒント:
- 摘要テキストの類似性（例: CSVの「アマゾン」と仕訳の「Amazon.co.jp」）
- 日付の近さ（クレジットカードは利用日と計上日にずれが生じやすい）
- 端数の違い（ポイント利用・割引で金額が僅かに異なるケース）
- 分割払いの合計と一括の対応

## CSV明細（未照合）
{csv_rows_text}

## 既存仕訳（未照合）
{journal_rows_text}

各CSV行に対して、最も可能性の高い仕訳候補を1件（確信度とともに）提案してください。
確信度が低い場合（0.3未満）は候補なしとしてください。

必ず以下のJSON形式のみを返してください。他のテキストは含めないでください。
{{"matches": [
  {{"csv_index": 0, "entry_id": 123, "confidence": 0.85, "reason": "摘要が類似"}},
  {{"csv_index": 1, "entry_id": null, "confidence": 0, "reason": "該当なし"}}
]}}"""

AI_RECONCILE_BATCH_SIZE = 30


def find_ai_matches(user_id, unmatched_csv, journal_candidates):
    """AIを使って金額不一致の照合候補を提案する。

    Args:
        user_id: ユーザーID
        unmatched_csv: list of dict — {csv_index, date, description, amount}
        journal_candidates: list of dict — {entry_id, date, description, amount, category_name}

    Returns:
        list of dict — {csv_index, entry_id, confidence, reason}
    """
    from app.services.ai_receipt import _get_ai_config, _TEXT_PROVIDER_HANDLERS

    api_key, provider, model, _, __, extra_kw, ___ = _get_ai_config(user_id)
    text_handler = _TEXT_PROVIDER_HANDLERS.get(provider)
    if not text_handler:
        raise ValueError(f"未対応のAIプロバイ���ーです: {provider}")

    if not unmatched_csv or not journal_candidates:
        return []

    journal_text = _format_journal_rows(journal_candidates)
    all_matches = []

    # バッチ処理
    for start in range(0, len(unmatched_csv), AI_RECONCILE_BATCH_SIZE):
        batch = unmatched_csv[start:start + AI_RECONCILE_BATCH_SIZE]
        csv_text = _format_csv_rows(batch)
        prompt = AI_RECONCILE_PROMPT.format(
            csv_rows_text=csv_text,
            journal_rows_text=journal_text,
        )
        # text_handler は内部で _extract_json() 済みの dict を返す
        parsed = text_handler(api_key, model, prompt, **extra_kw)
        if isinstance(parsed, dict) and "matches" in parsed:
            for m in parsed["matches"]:
                if m.get("entry_id") and m.get("confidence", 0) >= 0.3:
                    all_matches.append({
                        "csv_index": m["csv_index"],
                        "entry_id": m["entry_id"],
                        "confidence": m["confidence"],
                        "reason": m.get("reason", ""),
                    })

    return all_matches


def _format_csv_rows(rows):
    lines = []
    for r in rows:
        lines.append(
            f"[{r['csv_index']}] {r.get('date', '?')} "
            f"{r.get('description', '')} ¥{r.get('amount', 0):,}"
        )
    return "\n".join(lines)


def _format_journal_rows(rows):
    lines = []
    for r in rows:
        lines.append(
            f"[ID:{r['entry_id']}] {r.get('date', '?')} "
            f"{r.get('description', '')} ¥{r.get('amount', 0):,} "
            f"({r.get('category_name', '')})"
        )
    return "\n".join(lines)
