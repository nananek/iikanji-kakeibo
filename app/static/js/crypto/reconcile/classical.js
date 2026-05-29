// E3-F PR-D-2: CSV 明細の決定論的照合 (旧サーバ reconciliation.find_matches)。
//
// サーバ側 find_matches / _build_daily_summary / _classify_band は平文
// (journal_entries.date / description / source, line.account_code) を読むため、
// E2EE 化に伴いここへ移植した。入力の journalEntries は
// journals_client.fetchJournalsForYear で復号済みの正規化 entry を想定する
// (= {id, date, description, source, lines: [{account_code, debit, credit}]})。
//
// アルゴリズムは Python 版と 1:1 対応 (±7 日トレランス / 距離貪欲確定 /
// multiple 判定 / journal_only 範囲・方向フィルタ / 日計サマリー)。テストは
// tests/static/js/test_reconcile_classical.mjs が Python 版と同等のケースで
// カバーする。

// 照合対象とする日付差の上限 (これを超えると unmatched 扱い)。
export const MATCH_DATE_TOLERANCE = 7;

// バッジ階段の境界 (含む)。
export const MATCH_DATE_BAND_EXACT = 0; // 0 日: 完全一致
export const MATCH_DATE_BAND_WARN = 3; // 1〜3 日: 日付ズレ
export const MATCH_DATE_BAND_CAUTION = 7; // 4〜7 日: 要確認

const MS_PER_DAY = 86400000;


/** 日付差の絶対値からバッジカテゴリを返す。 */
export function classifyBand(diffDaysAbs) {
  if (diffDaysAbs <= MATCH_DATE_BAND_EXACT) return "exact";
  if (diffDaysAbs <= MATCH_DATE_BAND_WARN) return "warn";
  return "caution";
}


/**
 * "YYYY-MM-DD" を厳密にパースして {day, iso} を返す (不正なら null)。
 *
 * Python の date.fromisoformat と同等に "2026-02-30" 等の不正日を弾く。
 * day はエポックからの通日 (UTC 基準) で、日付差・範囲比較に使う。
 */
function _parseISODate(value) {
  if (typeof value !== "string") return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  const t = Date.UTC(y, mo - 1, d);
  const dt = new Date(t);
  // 桁あふれ (2/30 等) は round-trip で検出。
  if (
    dt.getUTCFullYear() !== y ||
    dt.getUTCMonth() !== mo - 1 ||
    dt.getUTCDate() !== d
  ) {
    return null;
  }
  const iso = `${m[1]}-${m[2]}-${m[3]}`;
  return { day: Math.floor(t / MS_PER_DAY), iso };
}


/** Date オブジェクトを UTC 日付の通日に変換 (today の正規化用)。 */
function _dayFromDate(dateObj) {
  return Math.floor(
    Date.UTC(
      dateObj.getUTCFullYear(),
      dateObj.getUTCMonth(),
      dateObj.getUTCDate(),
    ) / MS_PER_DAY,
  );
}


function _amount(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0;
}


function _resolveName(accountName, code) {
  if (typeof accountName === "function") return accountName(code) || "";
  if (accountName && typeof accountName === "object") return accountName[code] || "";
  return "";
}


/**
 * CSV parsed 行と既存仕訳のマッチングを行う (サーバ find_matches と等価)。
 *
 * @param {Object} args
 * @param {string} args.paymentAccountCode      支払元口座コード
 * @param {Array<Object>} args.csvRows          [{date, description, deposit, withdrawal}]
 * @param {Array<Object>} args.journalEntries   復号済み entry [{id, date, description, source, lines}]
 * @param {Function|Object} [args.accountName]  相手科目名解決 (code)=>name または {code:name}
 * @param {Date} [args.today]                   経過日数の基準 (省略時 new Date())
 * @returns {{csv_results: Array, journal_only: Array, daily_summary: Array}}
 */
export function findMatches({
  paymentAccountCode,
  csvRows,
  journalEntries,
  accountName,
  today,
}) {
  csvRows = csvRows || [];
  journalEntries = journalEntries || [];
  if (csvRows.length === 0) {
    return { csv_results: [], journal_only: [], daily_summary: [] };
  }

  // Phase 1: CSV 行から日付範囲を算出。
  const allDays = [];
  for (const row of csvRows) {
    const p = _parseISODate(row.date);
    if (p) allDays.push(p.day);
  }
  if (allDays.length === 0) {
    return {
      csv_results: csvRows.map((_, i) => ({
        csv_index: i, status: "unmatched", matches: [],
      })),
      journal_only: [],
      daily_summary: [],
    };
  }

  const csvDateMin = Math.min(...allDays);
  const csvDateMax = Math.max(...allDays);
  const dateMin = csvDateMin - MATCH_DATE_TOLERANCE;
  const dateMax = csvDateMax + MATCH_DATE_TOLERANCE;

  // CSV 行に含まれる方向 (withdrawal/deposit) を抽出。
  const csvDirections = new Set();
  for (const row of csvRows) {
    if (_amount(row.withdrawal) > 0) csvDirections.add("withdrawal");
    if (_amount(row.deposit) > 0) csvDirections.add("deposit");
  }

  // Phase 2/3: 支払元口座の仕訳行を抽出し、行単位ユニーク ID を付与。
  const lineCandidates = [];
  const entryById = new Map();
  for (const entry of journalEntries) {
    entryById.set(entry.id, entry);
    const parsed = _parseISODate(entry.date);
    if (!parsed) continue;
    if (parsed.day < dateMin || parsed.day > dateMax) continue;
    for (const line of entry.lines || []) {
      if (line.account_code !== paymentAccountCode) continue;
      lineCandidates.push({
        _line_id: lineCandidates.length,
        entry_id: entry.id,
        day: parsed.day,
        date: parsed.iso,
        description: entry.description ?? "",
        source: entry.source ?? "",
        credit: _amount(line.credit),
        debit: _amount(line.debit),
      });
    }
  }

  // Phase 4: 相手科目名をエントリ単位で収集。
  const candidateEntryIds = new Set(lineCandidates.map((c) => c.entry_id));
  const counterpartMap = new Map();
  for (const entryId of candidateEntryIds) {
    const entry = entryById.get(entryId);
    if (!entry) continue;
    const names = [];
    for (const line of entry.lines || []) {
      if (line.account_code === paymentAccountCode) continue;
      const name = _resolveName(accountName, line.account_code);
      if (name) names.push(name);
    }
    counterpartMap.set(entryId, names);
  }

  // Phase 5: 金額インデックス構築 (行単位)。
  const amountIndex = new Map();
  const _pushIndex = (amount, direction, cand) => {
    const key = `${amount}|${direction}`;
    if (!amountIndex.has(key)) amountIndex.set(key, []);
    amountIndex.get(key).push(cand);
  };
  for (const c of lineCandidates) {
    if (c.credit > 0) _pushIndex(c.credit, "withdrawal", c);
    if (c.debit > 0) _pushIndex(c.debit, "deposit", c);
  }

  // Phase 6a: 各 CSV 行についてトレランス内の全候補を列挙。
  const csvMeta = [];
  for (let i = 0; i < csvRows.length; i++) {
    const row = csvRows[i];
    const parsed = _parseISODate(row.date);
    if (!parsed) {
      csvMeta.push({ csv_index: i, csv_day: null, csv_date: null });
      continue;
    }
    const withdrawal = _amount(row.withdrawal);
    const deposit = _amount(row.deposit);
    let amount, direction;
    if (withdrawal > 0) {
      amount = withdrawal;
      direction = "withdrawal";
    } else if (deposit > 0) {
      amount = deposit;
      direction = "deposit";
    } else {
      csvMeta.push({ csv_index: i, csv_day: parsed.day, csv_date: parsed.iso });
      continue;
    }

    const potential = amountIndex.get(`${amount}|${direction}`) || [];
    const candidatesForRow = [];
    for (const c of potential) {
      const diff = parsed.day - c.day; // 符号付き: CSV - 仕訳
      if (Math.abs(diff) <= MATCH_DATE_TOLERANCE) {
        candidatesForRow.push({ line_id: c._line_id, diff, candidate: c });
      }
    }
    csvMeta.push({
      csv_index: i,
      csv_day: parsed.day,
      csv_date: parsed.iso,
      amount,
      direction,
      candidates: candidatesForRow,
    });
  }

  // Phase 6b: 距離が小さい順に貪欲確定 (早い者勝ち)。
  const pairPool = [];
  for (const meta of csvMeta) {
    if (!meta.candidates) continue;
    for (const cand of meta.candidates) {
      pairPool.push({
        absDiff: Math.abs(cand.diff),
        csvIndex: meta.csv_index,
        meta,
        cand,
      });
    }
  }
  pairPool.sort((a, b) => (a.absDiff - b.absDiff) || (a.csvIndex - b.csvIndex));

  const assignedCsv = new Map(); // csv_index -> cand
  const usedLineIds = new Set();
  for (const { meta, cand } of pairPool) {
    if (assignedCsv.has(meta.csv_index)) continue;
    if (usedLineIds.has(cand.line_id)) continue;
    assignedCsv.set(meta.csv_index, cand);
    usedLineIds.add(cand.line_id);
  }

  // Phase 6c: 結果リスト構築 + multiple 判定。
  const results = [];
  for (const meta of csvMeta) {
    const i = meta.csv_index;
    if (!meta.candidates || meta.csv_date === null || meta.candidates.length === 0) {
      results.push({ csv_index: i, status: "unmatched", matches: [] });
      continue;
    }

    const candidatesForRow = meta.candidates.slice().sort(
      (a, b) => Math.abs(a.diff) - Math.abs(b.diff),
    );
    const allMatches = [];
    for (const cand of candidatesForRow) {
      const c = cand.candidate;
      const catNames = counterpartMap.get(c.entry_id) || [];
      allMatches.push({
        _line_id: c._line_id,
        entry_id: c.entry_id,
        date: c.date,
        description: c.description,
        amount: meta.amount,
        source: c.source,
        category_name: catNames.join(", "),
        date_diff_days: cand.diff,
        date_band: classifyBand(Math.abs(cand.diff)),
      });
    }

    const assigned = assignedCsv.get(i);
    if (!assigned) {
      results.push({ csv_index: i, status: "unmatched", matches: [] });
      continue;
    }
    const myMatch = allMatches.find((m) => m._line_id === assigned.line_id);
    if (!myMatch) {
      results.push({ csv_index: i, status: "unmatched", matches: [] });
      continue;
    }

    const remaining = allMatches.filter(
      (m) => m._line_id === assigned.line_id || !usedLineIds.has(m._line_id),
    );
    remaining.sort((a, b) => {
      const ka = a._line_id !== assigned.line_id ? 1 : 0;
      const kb = b._line_id !== assigned.line_id ? 1 : 0;
      return (ka - kb) || (Math.abs(a.date_diff_days) - Math.abs(b.date_diff_days));
    });

    if (remaining.length >= 2) {
      results.push({ csv_index: i, status: "multiple", matches: remaining });
    } else {
      results.push({ csv_index: i, status: "matched", matches: [myMatch] });
    }
  }

  // Phase 7: CSV にマッチしなかった仕訳行を検出 (journal_only)。
  const referencedLineIds = new Set();
  for (const r of results) {
    for (const m of r.matches) referencedLineIds.add(m._line_id);
  }

  const todayDay = _dayFromDate(today instanceof Date ? today : new Date());
  const journalOnly = [];
  for (const c of lineCandidates) {
    if (referencedLineIds.has(c._line_id)) continue;
    // CSV 取込範囲外の仕訳は journal_only に含めない。
    if (c.day < csvDateMin || c.day > csvDateMax) continue;
    let amount, direction;
    if (c.credit > 0) {
      amount = c.credit;
      direction = "withdrawal";
    } else if (c.debit > 0) {
      amount = c.debit;
      direction = "deposit";
    } else {
      continue;
    }
    // CSV に出現しない方向の仕訳は未達対象外。
    if (csvDirections.size > 0 && !csvDirections.has(direction)) continue;
    const catNames = counterpartMap.get(c.entry_id) || [];
    const daysSince = todayDay - c.day;
    journalOnly.push({
      entry_id: c.entry_id,
      date: c.date,
      description: c.description,
      amount,
      direction,
      source: c.source,
      category_name: catNames.join(", "),
      days_since_journal: daysSince,
      is_stale: daysSince > 30,
    });
  }
  journalOnly.sort((a, b) => b.days_since_journal - a.days_since_journal);

  // Phase 8: 日計サマリーを構築。
  const dailySummary = _buildDailySummary(
    csvRows, results, lineCandidates, csvMeta, journalOnly,
  );

  // _line_id を出力から除去。
  for (const r of results) {
    for (const m of r.matches) delete m._line_id;
  }

  return {
    csv_results: results,
    journal_only: journalOnly,
    daily_summary: dailySummary,
  };
}


function _emptyBucket() {
  return { count: 0, withdrawal: 0, deposit: 0 };
}


/**
 * 日付ごとの CSV vs 仕訳の比較サマリーを生成する (Python _build_daily_summary)。
 */
function _buildDailySummary(csvRows, csvResults, candidates, csvMeta, journalOnly) {
  journalOnly = journalOnly || [];

  // CSV 側: 日付ごとの件数・出金・入金。
  const csvByDate = new Map();
  for (const row of csvRows) {
    const p = _parseISODate(row.date);
    if (!p) continue;
    if (!csvByDate.has(p.iso)) csvByDate.set(p.iso, _emptyBucket());
    const b = csvByDate.get(p.iso);
    b.count += 1;
    b.withdrawal += _amount(row.withdrawal);
    b.deposit += _amount(row.deposit);
  }

  // 仕訳側: 日付ごとの件数・貸方(出金)・借方(入金) (行単位)。
  const journalByDate = new Map();
  for (const c of candidates) {
    if (!journalByDate.has(c.date)) journalByDate.set(c.date, _emptyBucket());
    const b = journalByDate.get(c.date);
    b.count += 1;
    b.withdrawal += c.credit;
    b.deposit += c.debit;
  }

  // 日跨ぎマッチ件数 (CSV 側日付に集計)。
  const crossDayByCsvDate = new Map();
  const metaByCsvIndex = new Map(csvMeta.map((m) => [m.csv_index, m]));
  for (const r of csvResults) {
    if (r.status !== "matched") continue;
    const m = metaByCsvIndex.get(r.csv_index);
    if (!m || m.csv_date === null) continue;
    const match = r.matches.length ? r.matches[0] : null;
    if (match && (match.date_diff_days || 0) !== 0) {
      crossDayByCsvDate.set(m.csv_date, (crossDayByCsvDate.get(m.csv_date) || 0) + 1);
    }
  }

  // カード会社未達: その日の journal_only 合計。
  const pendingByDate = new Map();
  for (const j of journalOnly) {
    const p = _parseISODate(j.date);
    if (!p) continue;
    pendingByDate.set(p.iso, (pendingByDate.get(p.iso) || 0) + _amount(j.amount));
  }

  // 全日付を統合してサマリー生成。
  const allDates = Array.from(
    new Set([...csvByDate.keys(), ...journalByDate.keys()]),
  ).sort();

  const summary = [];
  for (const d of allDates) {
    const csvInfo = csvByDate.get(d) || _emptyBucket();
    const jnlInfo = journalByDate.get(d) || _emptyBucket();
    const csvTotal = csvInfo.withdrawal + csvInfo.deposit;
    const jnlTotal = jnlInfo.withdrawal + jnlInfo.deposit;
    const diffAmount = csvTotal - jnlTotal;
    const diffCount = csvInfo.count - jnlInfo.count;
    summary.push({
      date: d,
      csv_count: csvInfo.count,
      csv_withdrawal: csvInfo.withdrawal,
      csv_deposit: csvInfo.deposit,
      csv_total: csvTotal,
      journal_count: jnlInfo.count,
      journal_withdrawal: jnlInfo.withdrawal,
      journal_deposit: jnlInfo.deposit,
      journal_total: jnlTotal,
      diff_count: diffCount,
      diff_amount: diffAmount,
      has_discrepancy: diffAmount !== 0 || diffCount !== 0,
      cross_day_matched: crossDayByCsvDate.get(d) || 0,
      pending_card_amount: pendingByDate.get(d) || 0,
    });
  }

  return summary;
}
