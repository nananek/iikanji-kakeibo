// Phase E3-E-3: balance_cache 集計のクライアントサイド純粋関数。
//
// サーバ側 app/services/balance_cache.compute_balance_cache と等価のロジックを
// クライアントで再実装する。月次確定後に syncBalanceCacheForPeriod が呼び出して
// 結果を暗号化 → PUT する用途。
//
// 計算ルール (compute_balance_cache と同等):
//   - 集計対象: fiscal_period <= period の entries
//     fiscal_period が null/未設定 (旧データ救済) なら date の月を使う
//   - source="closing" の仕訳は period >= 16 (= includeClosing=true) でのみ含める
//   - 結果は {account_code: [debit, credit]} の dict、両方 0 の account は除外


function _effectivePeriod(entry) {
  const fp = entry.fiscal_period;
  if (typeof fp === "number") return fp;
  // NULL/未設定: date の月にフォールバック (サーバ period_condition と同じ救済)
  // 新規 entries は fiscal_period 必須なので発生しないはずだが、旧データ対策
  const d = entry.date;
  if (typeof d === "string" && /^\d{4}-\d{2}/.test(d)) {
    return parseInt(d.substring(5, 7), 10);
  }
  return 0;
}


/**
 * balance_cache を計算する純粋関数。
 *
 * @param {Array<Object>} entries
 *   journals_client.fetchJournalsForYear の戻り値形式
 *   [{fiscal_period, source, date, lines: [{account_code, debit, credit}]}]
 * @param {Object} options
 * @param {number} options.period         0..16
 * @param {boolean} [options.includeClosing]
 *   省略時は period >= 16 で true (サーバ側 include_closing と同じ)
 *
 * @returns {Object<string, [number, number]>}
 *   {account_code: [debit, credit]}, 両方 0 の account は除外
 */
export function computeBalanceCache(entries, options) {
  if (!Array.isArray(entries)) {
    throw new TypeError("entries must be an array");
  }
  if (!options || !Number.isInteger(options.period)) {
    throw new TypeError("options.period (integer) is required");
  }
  if (options.period < 0 || options.period > 16) {
    throw new TypeError("options.period must be 0..16");
  }
  const period = options.period;
  const includeClosing = options.includeClosing ?? (period >= 16);

  const sums = new Map();  // code -> [debit, credit]
  for (const entry of entries) {
    const fp = _effectivePeriod(entry);
    if (fp > period) continue;
    if (!includeClosing && entry.source === "closing") continue;
    for (const line of entry.lines || []) {
      const code = line.account_code;
      if (code == null) continue;
      const cur = sums.get(code) ?? [0, 0];
      cur[0] += line.debit ?? 0;
      cur[1] += line.credit ?? 0;
      sums.set(code, cur);
    }
  }

  const result = {};
  for (const [code, [d, c]] of sums.entries()) {
    if (d === 0 && c === 0) continue;
    result[code] = [d, c];
  }
  return result;
}
