// Phase E3-F-4d: ダッシュボード集計の純粋関数。
//
// fetchJournalsForYear の正規化 entries 配列 (1 年分) と
// accountsMeta (code → {type}) から、サーバの get_income_expense_summary
// と等価な月別 / 年累計サマリを返す。
//
// サーバ仕様 (app/services/tax.get_income_expense_summary):
//   - 月別: 当該月の収益 (income) - 費用 (expense)
//   - 年累計: 当年全期間 (fiscal_month 0..15, closing 除外)
//   - 月別の判定は **JournalEntry.date.month** ベースだった。クライアントは
//     date が暗号化される将来を見据え、fiscal_month (1..12) で月を判定
//     する (試算表/月次比較と同じ方針)。fp=0 (期首振戻) と fp=13..15
//     (決算整理) と fp=16 (損益振替) は月別合計から除外。年累計には
//     fp=0..15 を含める (E2EE 化前 server 仕様と最も近い)。


export const ALLOWED_FP_YEARLY = new Set([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]);


/**
 * @param {Array<Object>} entries 1 年分の正規化 entries
 * @param {Object} accountsMeta {[code]: {type}}
 * @param {Object} options
 * @param {number} options.month   1..12 — このとき月別サマリも返す
 * @param {number} [options.untilMonth]  monthly_trend 用: 1..untilMonth の集計
 * @returns {Object}
 *   {
 *     monthly: { income, expense, balance },     // options.month=M の月
 *     yearly:  { income, expense, balance },     // 年累計
 *     monthly_trend: [{month, income, expense}], // 1..untilMonth (income/expense のみ)
 *   }
 */
export function composeDashboardView(entries, accountsMeta, options = {}) {
  if (!Array.isArray(entries)) {
    throw new TypeError("entries must be an array");
  }
  if (!accountsMeta || typeof accountsMeta !== "object") {
    throw new TypeError("accountsMeta must be an object");
  }
  const month = options.month;
  const untilMonth = options.untilMonth ?? 0;

  // 月別集計バケツ (1..12) — income / expense net
  const byMonth = [];
  for (let i = 0; i < 13; i++) byMonth.push({ income: 0, expense: 0 });
  let yearlyIncome = 0;
  let yearlyExpense = 0;

  for (const entry of entries) {
    // E3-F PR-D-6-3b: 平文 source / fiscal_period は API から撤去済。closing 判定は
    // is_closing、期間判定は保持列 fiscal_month を使う。
    if (entry.is_closing) continue;
    const fp = entry.fiscal_month;
    if (typeof fp !== "number") continue;
    if (!ALLOWED_FP_YEARLY.has(fp)) continue;

    let entryIncomeNet = 0;
    let entryExpenseNet = 0;
    for (const line of entry.lines || []) {
      const code = line.account_code;
      if (!code) continue;
      const meta = accountsMeta[code];
      if (!meta) continue;
      if (meta.type === "revenue") {
        entryIncomeNet += (line.credit || 0) - (line.debit || 0);
      } else if (meta.type === "expense") {
        entryExpenseNet += (line.debit || 0) - (line.credit || 0);
      }
    }
    yearlyIncome += entryIncomeNet;
    yearlyExpense += entryExpenseNet;
    if (fp >= 1 && fp <= 12) {
      byMonth[fp].income += entryIncomeNet;
      byMonth[fp].expense += entryExpenseNet;
    }
  }

  const monthly = month != null && month >= 1 && month <= 12
    ? {
      income: byMonth[month].income,
      expense: byMonth[month].expense,
      balance: byMonth[month].income - byMonth[month].expense,
    }
    : { income: 0, expense: 0, balance: 0 };

  const yearly = {
    income: yearlyIncome,
    expense: yearlyExpense,
    balance: yearlyIncome - yearlyExpense,
  };

  const monthly_trend = [];
  const upTo = untilMonth >= 1 && untilMonth <= 12 ? untilMonth : 0;
  for (let m = 1; m <= upTo; m++) {
    monthly_trend.push({
      month: m,
      income: byMonth[m].income,
      expense: byMonth[m].expense,
    });
  }

  return { monthly, yearly, monthly_trend };
}
