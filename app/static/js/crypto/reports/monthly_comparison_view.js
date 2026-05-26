// Phase E3-F-3d: 月次比較 view 構築の純粋関数。
//
// computeMonthlyComparison の結果 (家計+事業を含む) と
// accountsMeta (is_business, cost_type 含む) から、
// - 家計簿向け income/expense 一覧 (事業科目を除外)
// - 事業所得月次 (biz_monthly)
// - 収入/支出区分分析 (固定/変動/臨時)
// を組み立てる。


/**
 * @param {Object} jsResult computeMonthlyComparison の戻り値
 * @param {Object} accountsMeta {[code]: {type, name, cost_type, is_business}}
 * @returns {Object}
 */
export function composeMonthlyComparisonView(jsResult, accountsMeta) {
  if (!jsResult || typeof jsResult !== "object") {
    throw new TypeError("jsResult must be an object");
  }
  if (!accountsMeta || typeof accountsMeta !== "object") {
    throw new TypeError("accountsMeta must be an object");
  }
  if (!Array.isArray(jsResult.income_accounts)
      || !Array.isArray(jsResult.expense_accounts)) {
    throw new TypeError("jsResult.income_accounts / expense_accounts must be arrays");
  }

  // 事業科目を除外しつつ cost_type を埋め込む
  const householdIncome = [];
  const householdExpense = [];
  const bizRevenueMonths = Array(12).fill(0);
  const bizExpenseMonths = Array(12).fill(0);

  function decorate(a) {
    const meta = accountsMeta[a.code] || {};
    return Object.assign({}, a, {
      name: meta.name || a.name || a.code,
      cost_type: meta.cost_type || "occasional",
    });
  }

  for (const a of jsResult.income_accounts) {
    const meta = accountsMeta[a.code];
    if (meta && meta.is_business) {
      for (let i = 0; i < 12; i++) bizRevenueMonths[i] += a.months[i] || 0;
    } else {
      householdIncome.push(decorate(a));
    }
  }
  for (const a of jsResult.expense_accounts) {
    const meta = accountsMeta[a.code];
    if (meta && meta.is_business) {
      for (let i = 0; i < 12; i++) bizExpenseMonths[i] += a.months[i] || 0;
    } else {
      householdExpense.push(decorate(a));
    }
  }

  const incomeTotals = Array(12).fill(0);
  for (const a of householdIncome) {
    for (let i = 0; i < 12; i++) incomeTotals[i] += a.months[i] || 0;
  }
  const expenseTotals = Array(12).fill(0);
  for (const a of householdExpense) {
    for (let i = 0; i < 12; i++) expenseTotals[i] += a.months[i] || 0;
  }
  const netTotals = incomeTotals.map((v, i) => v - expenseTotals[i]);

  // biz_monthly: 事業所得 = 事業収益 - 事業費用 (月次配列)
  const bizMonths = Array(12).fill(0)
    .map((_v, i) => bizRevenueMonths[i] - bizExpenseMonths[i]);
  const bizTotal = bizMonths.reduce((s, v) => s + v, 0);
  const bizMonthly = bizTotal !== 0 || bizMonths.some((v) => v !== 0)
    ? { months: bizMonths, total: bizTotal }
    : null;

  // 区分分析 (固定/変動/臨時)
  const buckets = {
    income: { fixed: 0, variable: 0, occasional: 0 },
    expense: { fixed: 0, variable: 0, occasional: 0 },
  };
  const monthly = {
    income: { fixed: Array(12).fill(0), variable: Array(12).fill(0), occasional: Array(12).fill(0) },
    expense: { fixed: Array(12).fill(0), variable: Array(12).fill(0), occasional: Array(12).fill(0) },
  };
  for (const a of householdIncome) {
    const ct = ["fixed", "variable", "occasional"].includes(a.cost_type)
      ? a.cost_type : "occasional";
    buckets.income[ct] += a.total || 0;
    for (let i = 0; i < 12; i++) monthly.income[ct][i] += a.months[i] || 0;
  }
  for (const a of householdExpense) {
    const ct = ["fixed", "variable", "occasional"].includes(a.cost_type)
      ? a.cost_type : "occasional";
    buckets.expense[ct] += a.total || 0;
    for (let i = 0; i < 12; i++) monthly.expense[ct][i] += a.months[i] || 0;
  }

  return {
    income_accounts: householdIncome,
    expense_accounts: householdExpense,
    income_totals: incomeTotals,
    expense_totals: expenseTotals,
    net_totals: netTotals,
    biz_monthly: bizMonthly,
    breakdown: {
      income_totals: buckets.income,
      expense_totals: buckets.expense,
      income_monthly: monthly.income,
      expense_monthly: monthly.expense,
      income_grand: buckets.income.fixed + buckets.income.variable + buckets.income.occasional,
      expense_grand: buckets.expense.fixed + buckets.expense.variable + buckets.expense.occasional,
    },
  };
}
