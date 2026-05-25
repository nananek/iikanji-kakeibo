// Phase E3-C-5: 月次比較 (Monthly Comparison) のクライアントサイド集計。
//
// 1 年分の entries を 12 ヶ月 × 科目別の pivot テーブルに集計する純粋関数。
// 各月の収益/費用合計も同時に算出。
//
// サーバ側 app/services/tax.get_monthly_comparison と並存。UI 統合は別 PR
// (E3-C-5b)、サーバ側削除は Phase E7 (E3-F)。
//
// 設計上の制約:
//   サーバ側は date.month で月を判定するが、クライアントは date が暗号化
//   されているため fiscal_period (1..12) で月を判定する。
//   - fp=0 (期首振戻)、fp=13..15 (決算整理)、fp=16 (損益振替)、source=closing
//     は月次比較から除外する。これらは年度内全期間集計には P/L 関数で
//     拾えるが、12 ヶ月の月別棒グラフという表示形式には乗らない。

/**
 * 12 ヶ月の月次比較を計算。
 *
 * @param {Array<Object>} entries
 *   1 年分の正規化 entry 配列 (journals_client の戻り値)
 * @param {Object} options
 * @param {Object} options.accountTypeByCode
 *   {[code]: "revenue"|"expense"|...}
 * @param {Object} [options.accountNameByCode]
 *
 * @returns {Object} {
 *   expense_accounts: [{code, name, months: [12], total}],
 *   income_accounts:  [{code, name, months: [12], total}],
 *   expense_totals:   [12],      // 各月の費用合計
 *   income_totals:    [12],      // 各月の収益合計
 *   net_totals:       [12],      // 各月の (income - expense)
 * }
 */
export function computeMonthlyComparison(entries, options) {
  if (!Array.isArray(entries)) {
    throw new TypeError("entries must be an array");
  }
  if (!options || !options.accountTypeByCode) {
    throw new TypeError("options.accountTypeByCode is required");
  }
  const {
    accountTypeByCode,
    accountNameByCode = {},
  } = options;

  // accountCode → { type, months: [12] のシンプル累積 net 値, total }
  const expenseByCode = new Map();
  const incomeByCode = new Map();

  for (const entry of entries) {
    if (entry.source === "closing") continue;
    const fp = entry.fiscal_period;
    if (typeof fp !== "number") continue;
    if (fp < 1 || fp > 12) continue;  // 期首/決算整理/振替は除外
    const monthIdx = fp - 1;

    for (const line of entry.lines || []) {
      const code = line.account_code;
      if (code == null) continue;
      const type = accountTypeByCode[code];
      if (type === "expense") {
        const cur = expenseByCode.get(code) ?? {
          code, name: accountNameByCode[code] ?? code,
          months: Array(12).fill(0), total: 0,
        };
        // 費用 = debit - credit
        const net = (line.debit ?? 0) - (line.credit ?? 0);
        cur.months[monthIdx] += net;
        cur.total += net;
        expenseByCode.set(code, cur);
      } else if (type === "revenue") {
        const cur = incomeByCode.get(code) ?? {
          code, name: accountNameByCode[code] ?? code,
          months: Array(12).fill(0), total: 0,
        };
        // 収益 = credit - debit
        const net = (line.credit ?? 0) - (line.debit ?? 0);
        cur.months[monthIdx] += net;
        cur.total += net;
        incomeByCode.set(code, cur);
      }
    }
  }

  const expense_accounts = [...expenseByCode.values()]
    .sort((a, b) => a.code.localeCompare(b.code));
  const income_accounts = [...incomeByCode.values()]
    .sort((a, b) => a.code.localeCompare(b.code));

  const expense_totals = Array(12).fill(0);
  for (const a of expense_accounts) {
    for (let i = 0; i < 12; i++) expense_totals[i] += a.months[i];
  }
  const income_totals = Array(12).fill(0);
  for (const a of income_accounts) {
    for (let i = 0; i < 12; i++) income_totals[i] += a.months[i];
  }
  const net_totals = income_totals.map((v, i) => v - expense_totals[i]);

  return {
    expense_accounts,
    income_accounts,
    expense_totals,
    income_totals,
    net_totals,
  };
}
