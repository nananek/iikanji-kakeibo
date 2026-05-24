"""CSV明細照合（マッチング）サービス"""

from collections import defaultdict
from datetime import date, timedelta

from app.extensions import db
from app.models.account import Account
from app.models.journal import JournalEntry, JournalEntryLine

# 照合対象とする日付差の上限（これを超えると unmatched 扱い）
MATCH_DATE_TOLERANCE = 7

# バッジ階段の境界（含む）
MATCH_DATE_BAND_EXACT = 0   # 0 日: 完全一致
MATCH_DATE_BAND_WARN = 3    # 1〜3 日: 日付ズレ
MATCH_DATE_BAND_CAUTION = 7  # 4〜7 日: 要確認


def _classify_band(diff_days_abs: int) -> str:
    """日付差の絶対値からバッジカテゴリを返す。"""
    if diff_days_abs <= MATCH_DATE_BAND_EXACT:
        return "exact"
    if diff_days_abs <= MATCH_DATE_BAND_WARN:
        return "warn"
    return "caution"


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

    csv_date_min = min(all_dates)
    csv_date_max = max(all_dates)
    date_min = csv_date_min - timedelta(days=MATCH_DATE_TOLERANCE)
    date_max = csv_date_max + timedelta(days=MATCH_DATE_TOLERANCE)

    # CSV 行に含まれる方向 (withdrawal/deposit) を抽出。
    # 「カード会社未達」は CSV で取り込まれる予定の方向の取引に限定する。
    # 例: クレカ明細 CSV は出金のみが載るため、引き落とし仕訳
    # (現金支払・銀行から CC 未払金への振替) は未達対象外。
    csv_directions = set()
    for row in csv_rows:
        if int(row.get("withdrawal") or 0) > 0:
            csv_directions.add("withdrawal")
        if int(row.get("deposit") or 0) > 0:
            csv_directions.add("deposit")

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

    # Phase 3: 各行にユニークIDを付与（同一entry_idの複数行を個別にマッチング可能に）
    line_candidates = []
    for idx, c in enumerate(candidates):
        credit = int(c.credit_amount) if c.credit_amount else 0
        debit = int(c.debit_amount) if c.debit_amount else 0
        line_candidates.append({
            "_line_id": idx,  # 行単位のユニークID
            "entry_id": c.entry_id,
            "date": c.date,
            "description": c.description,
            "source": c.source,
            "credit": credit,
            "debit": debit,
        })

    # Phase 4: 相手科目名をバッチ取得
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

    # Phase 5: 金額インデックス構築（行単位）
    amount_index = defaultdict(list)
    for c in line_candidates:
        if c["credit"] > 0:
            amount_index[(c["credit"], "withdrawal")].append(c)
        if c["debit"] > 0:
            amount_index[(c["debit"], "deposit")].append(c)

    # Phase 6a: 各 CSV 行について、トレランス内の全候補を列挙する（確定はしない）
    csv_meta = []  # {csv_index, csv_date, amount, direction, candidates}
    for i, row in enumerate(csv_rows):
        raw_date = row.get("date")
        if not raw_date:
            csv_meta.append({"csv_index": i, "csv_date": None})
            continue

        if isinstance(raw_date, str):
            try:
                csv_date = date.fromisoformat(raw_date)
            except ValueError:
                csv_meta.append({"csv_index": i, "csv_date": None})
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
            csv_meta.append({"csv_index": i, "csv_date": csv_date})
            continue

        potential = amount_index.get((amount, direction), [])
        candidates_for_row = []
        for c in potential:
            diff = (csv_date - c["date"]).days  # 符号付き: CSV - 仕訳
            if abs(diff) <= MATCH_DATE_TOLERANCE:
                candidates_for_row.append({
                    "line_id": c["_line_id"],
                    "diff": diff,
                    "candidate": c,
                })

        csv_meta.append({
            "csv_index": i,
            "csv_date": csv_date,
            "amount": amount,
            "direction": direction,
            "candidates": candidates_for_row,
        })

    # Phase 6b: 距離が小さい順に貪欲確定
    # 各 (csv_index, line_id) ペアを距離で全列挙してソート → 早い者勝ち
    pair_pool = []
    for meta in csv_meta:
        if "candidates" not in meta:
            continue
        for cand in meta["candidates"]:
            pair_pool.append((
                abs(cand["diff"]),  # 第一キー: 距離
                meta["csv_index"],  # 第二キー: CSV 順（安定的な tiebreaker）
                meta,
                cand,
            ))
    pair_pool.sort(key=lambda x: (x[0], x[1]))

    assigned_csv = {}  # csv_index -> {line_id, diff, candidate}
    used_line_ids = set()
    for _, _, meta, cand in pair_pool:
        if meta["csv_index"] in assigned_csv:
            continue
        if cand["line_id"] in used_line_ids:
            continue
        assigned_csv[meta["csv_index"]] = cand
        used_line_ids.add(cand["line_id"])

    # Phase 6c: 結果リスト構築 + multiple 判定
    results = []
    for meta in csv_meta:
        i = meta["csv_index"]
        if "candidates" not in meta or meta.get("csv_date") is None or not meta["candidates"]:
            results.append({"csv_index": i, "status": "unmatched", "matches": []})
            continue

        csv_date = meta["csv_date"]
        amount = meta["amount"]
        candidates_for_row = meta["candidates"]

        # 全候補を matches 形式で構築（距離小さい順）
        candidates_for_row.sort(key=lambda x: abs(x["diff"]))
        all_matches = []
        for cand in candidates_for_row:
            c = cand["candidate"]
            cat_names = counterpart_map.get(c["entry_id"], [])
            diff = cand["diff"]
            all_matches.append({
                "_line_id": c["_line_id"],
                "entry_id": c["entry_id"],
                "date": c["date"].isoformat(),
                "description": c["description"],
                "amount": amount,
                "source": c["source"],
                "category_name": ", ".join(cat_names),
                "date_diff_days": diff,
                "date_band": _classify_band(abs(diff)),
            })

        # 当該 CSV 行が貪欲法で確定したか？
        assigned = assigned_csv.get(i)
        if assigned is None:
            # 他の CSV 行に全候補を奪われた → unmatched
            results.append({"csv_index": i, "status": "unmatched", "matches": []})
            continue

        # 確定した候補のみを残し、残候補（別 CSV 行に取られたもの）は除外
        my_match = next(
            (m for m in all_matches if m["_line_id"] == assigned["line_id"]),
            None,
        )
        if my_match is None:
            results.append({"csv_index": i, "status": "unmatched", "matches": []})
            continue

        remaining = [
            m for m in all_matches
            if m["_line_id"] == assigned["line_id"]
            or m["_line_id"] not in used_line_ids
        ]
        # 確定したものを先頭に
        remaining.sort(key=lambda m: (m["_line_id"] != assigned["line_id"], abs(m["date_diff_days"])))

        if len(remaining) >= 2:
            results.append({"csv_index": i, "status": "multiple", "matches": remaining})
        else:
            results.append({"csv_index": i, "status": "matched", "matches": [my_match]})

    # Phase 7: CSV にマッチしなかった仕訳行を検出 (journal_only)
    # 「カード会社未達」= レシート起票済みだが CSV にまだ反映されていない仕訳
    referenced_line_ids = set()
    for r in results:
        for m in r["matches"]:
            referenced_line_ids.add(m.get("_line_id"))

    today = date.today()
    journal_only = []
    for c in line_candidates:
        if c["_line_id"] in referenced_line_ids:
            continue
        # CSV 取込範囲外の仕訳は journal_only に含めない:
        # csv_date_min より古いものは前回 CSV で照合済みのはず、
        # csv_date_max より新しいものは未来取込予定で今回の判断材料にならない。
        # マッチング候補としては ±7 日のトレランス範囲で取得しているが、
        # 「カード会社未達」として報告するのは CSV 範囲内に限る。
        if c["date"] < csv_date_min or c["date"] > csv_date_max:
            continue
        if c["credit"] > 0:
            amount, direction = c["credit"], "withdrawal"
        elif c["debit"] > 0:
            amount, direction = c["debit"], "deposit"
        else:
            continue
        # CSV に出現しない方向の仕訳は未達対象外 (引き落としや返金等)
        if csv_directions and direction not in csv_directions:
            continue
        cat_names = counterpart_map.get(c["entry_id"], [])
        days_since = (today - c["date"]).days
        journal_only.append({
            "entry_id": c["entry_id"],
            "date": c["date"].isoformat(),
            "description": c["description"],
            "amount": amount,
            "direction": direction,
            "source": c["source"],
            "category_name": ", ".join(cat_names),
            "days_since_journal": days_since,
            "is_stale": days_since > 30,
        })
    # 経過日数の降順（古いほど目立つ）
    journal_only.sort(key=lambda j: -j["days_since_journal"])

    # Phase 8: 日計サマリーを構築（日跨ぎマッチ件数 + カード未達内訳）
    daily_summary = _build_daily_summary(
        csv_rows, results, line_candidates, csv_meta, journal_only
    )

    # _line_id を JSON 出力から除去
    for r in results:
        for m in r["matches"]:
            m.pop("_line_id", None)

    return {
        "csv_results": results,
        "journal_only": journal_only,
        "daily_summary": daily_summary,
    }


def _build_daily_summary(csv_rows, csv_results, candidates, csv_meta,
                         journal_only=None):
    """日付ごとのCSV vs 仕訳の比較サマリーを生成する。

    集計は CSV/仕訳それぞれの本来日付で行う（日跨ぎマッチを片側にコピーしない）。
    日跨ぎマッチした件数は cross_day_matched として CSV 日付側の行に記録する。
    pending_card_amount はその日の journal_only 合計（カード会社未達金額）を表す。
    """
    if journal_only is None:
        journal_only = []
    # CSV側: 日付ごとの件数・出金合計・入金合計
    csv_by_date = defaultdict(lambda: {"count": 0, "withdrawal": 0, "deposit": 0})
    for row in csv_rows:
        d = row.get("date")
        if not d:
            continue
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except ValueError:
                continue
        csv_by_date[d]["count"] += 1
        csv_by_date[d]["withdrawal"] += int(row.get("withdrawal") or 0)
        csv_by_date[d]["deposit"] += int(row.get("deposit") or 0)

    # 仕訳側: 日付ごとの件数・貸方(出金)合計・借方(入金)合計（行単位）
    journal_by_date = defaultdict(lambda: {"count": 0, "withdrawal": 0, "deposit": 0})
    for c in candidates:
        journal_by_date[c["date"]]["count"] += 1
        journal_by_date[c["date"]]["withdrawal"] += c["credit"]
        journal_by_date[c["date"]]["deposit"] += c["debit"]

    # 日跨ぎマッチ件数（CSV 側日付に集計）
    cross_day_by_csv_date = defaultdict(int)
    meta_by_csv_index = {m["csv_index"]: m for m in csv_meta}
    for r in csv_results:
        if r["status"] != "matched":
            continue
        m = meta_by_csv_index.get(r["csv_index"])
        if not m or m.get("csv_date") is None:
            continue
        match = r["matches"][0] if r["matches"] else None
        if match and match.get("date_diff_days", 0) != 0:
            cross_day_by_csv_date[m["csv_date"]] += 1

    # カード会社未達: その日の journal_only 合計
    pending_by_date = defaultdict(int)
    for j in journal_only:
        try:
            jd = date.fromisoformat(j["date"])
        except (ValueError, TypeError):
            continue
        pending_by_date[jd] += int(j.get("amount") or 0)

    # 全日付を統合してサマリー生成
    all_dates = sorted(set(csv_by_date.keys()) | set(journal_by_date.keys()))
    summary = []
    for d in all_dates:
        csv_info = csv_by_date.get(d, {"count": 0, "withdrawal": 0, "deposit": 0})
        jnl_info = journal_by_date.get(d, {"count": 0, "withdrawal": 0, "deposit": 0})
        csv_total = csv_info["withdrawal"] + csv_info["deposit"]
        jnl_total = jnl_info["withdrawal"] + jnl_info["deposit"]
        diff_amount = csv_total - jnl_total
        diff_count = csv_info["count"] - jnl_info["count"]
        summary.append({
            "date": d.isoformat(),
            "csv_count": csv_info["count"],
            "csv_withdrawal": csv_info["withdrawal"],
            "csv_deposit": csv_info["deposit"],
            "csv_total": csv_total,
            "journal_count": jnl_info["count"],
            "journal_withdrawal": jnl_info["withdrawal"],
            "journal_deposit": jnl_info["deposit"],
            "journal_total": jnl_total,
            "diff_count": diff_count,
            "diff_amount": diff_amount,
            "has_discrepancy": diff_amount != 0 or diff_count != 0,
            "cross_day_matched": cross_day_by_csv_date.get(d, 0),
            "pending_card_amount": pending_by_date.get(d, 0),
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


# E2 PR-C-6c: クライアント完結 reconcile 用のプレースホルダ版。
# クライアントが csv_rows_text / journal_rows_text を構築 + 置換する。
AI_RECONCILE_PROMPT_TEMPLATE = """\
あなたは日本の家計簿アプリの照合アシスタントです。
以下はクレジットカード等のCSV明細と、既存の仕訳一覧です。
金額が完全一致しないものの、同一取引である可能性があるペアを見つけてください。

照合のヒント:
- 摘要テキストの類似性（例: CSVの「アマゾン」と仕訳の「Amazon.co.jp」）
- 日付の近さ（クレジットカードは利用日と計上日にずれが生じやすい）
- 端数の違い（ポイント利用・割引で金額が僅かに異なるケース）
- 分割払いの合計と一括の対応

## CSV明細（未照合）
__CSV_ROWS_TEXT__

## 既存仕訳（未照合）
__JOURNAL_ROWS_TEXT__

各CSV行に対して、最も可能性の高い仕訳候補を1件（確信度とともに）提案してください。
確信度が低い場合（0.3未満）は候補なしとしてください。

必ず以下のJSON形式のみを返してください。他のテキストは含めないでください。
{"matches": [
  {"csv_index": 0, "entry_id": 123, "confidence": 0.85, "reason": "摘要が類似"},
  {"csv_index": 1, "entry_id": null, "confidence": 0, "reason": "該当なし"}
]}"""

AI_RECONCILE_BATCH_SIZE = 30


# E2 PR-C-6c: find_ai_matches は E2EE 化に伴い削除。
# クライアント側 reconcile_orchestrator.js が等価の処理を実行する。
# 旧 AI_RECONCILE_PROMPT (Python str.format) も dead code として残る
# (caller 無し、後続 PR で削除可)。


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
