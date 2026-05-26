// Phase E3-F-3b: P/L の view 構築純粋関数。
//
// computeProfitLoss の戻り値と「事業所得」(biz_income, サーバ集計の
// 現時点キャリーオーバー) を結合し、summary cards + breakdown 表示用の
// view を返す。
//
// 戻り値:
//   {
//     summary: { income, expense, balance },
//     bizIncome: { has_mappings, income } | null,
//     income_breakdown: [{account_code, account_name, amount}],
//     expense_breakdown: [{account_code, account_name, amount}],
//   }


/**
 * P/L の view を組み立てる。
 *
 * @param {Object} jsResult computeProfitLoss の戻り値
 *   { income_total, expense_total, net_income, income_breakdown, expense_breakdown }
 * @param {Object} [options]
 * @param {Object} [options.bizIncome]
 *   { has_mappings: boolean, income: number } のいずれか。
 *   has_mappings === true かつ income !== 0 のときのみ「事業所得」行を
 *   income_breakdown に積み増しし、summary.income にも合算する。
 *   summary.balance は (summary.income - summary.expense) で再計算。
 * @returns {Object}
 */
export function composeProfitLossView(jsResult, options = {}) {
  if (!jsResult || typeof jsResult !== "object") {
    throw new TypeError("jsResult must be an object");
  }
  if (!Array.isArray(jsResult.income_breakdown)
      || !Array.isArray(jsResult.expense_breakdown)) {
    throw new TypeError("jsResult.income_breakdown / expense_breakdown must be arrays");
  }
  const biz = options.bizIncome || null;
  const showBiz = !!(biz && biz.has_mappings && biz.income);

  const income_total = (jsResult.income_total || 0) + (showBiz ? biz.income : 0);
  const expense_total = jsResult.expense_total || 0;

  return {
    summary: {
      income: income_total,
      expense: expense_total,
      balance: income_total - expense_total,
    },
    bizIncome: showBiz ? { income: biz.income } : null,
    income_breakdown: jsResult.income_breakdown.slice(),
    expense_breakdown: jsResult.expense_breakdown.slice(),
  };
}
