// Phase E3-F-3d: composeMonthlyComparisonView の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/monthly_comparison_view.js",
  import.meta.url,
);
const { composeMonthlyComparisonView } = await import(M.href);


// --- helper ---

function months(arr) {
  const result = Array(12).fill(0);
  for (const [i, v] of arr) result[i] = v;
  return result;
}

function jsResult({
  income_accounts = [], expense_accounts = [],
} = {}) {
  return {
    income_accounts, expense_accounts,
    income_totals: Array(12).fill(0),
    expense_totals: Array(12).fill(0),
    net_totals: Array(12).fill(0),
  };
}


// --- argument validation ---

test("jsResult が object でないと TypeError", () => {
  assert.throws(() => composeMonthlyComparisonView(null, {}), /object/);
});

test("accountsMeta が object でないと TypeError", () => {
  assert.throws(() => composeMonthlyComparisonView(jsResult(), null), /object/);
});


// --- household / business split ---

test("is_business=true の科目は household から除外され biz_monthly に集計", () => {
  const v = composeMonthlyComparisonView(jsResult({
    income_accounts: [
      { code: "4010", name: "給与", months: months([[0, 100000], [1, 100000]]), total: 200000 },
      { code: "9010", name: "売上", months: months([[0, 50000]]), total: 50000 },
    ],
    expense_accounts: [
      { code: "5010", name: "食費", months: months([[0, 10000]]), total: 10000 },
      { code: "9210", name: "事業経費", months: months([[0, 5000]]), total: 5000 },
    ],
  }), {
    "4010": { type: "revenue", name: "給与", cost_type: "fixed", is_business: false },
    "9010": { type: "revenue", name: "売上", cost_type: "variable", is_business: true },
    "5010": { type: "expense", name: "食費", cost_type: "variable", is_business: false },
    "9210": { type: "expense", name: "事業経費", cost_type: "occasional", is_business: true },
  });
  assert.equal(v.income_accounts.length, 1);
  assert.equal(v.income_accounts[0].code, "4010");
  assert.equal(v.expense_accounts.length, 1);
  assert.equal(v.expense_accounts[0].code, "5010");
  // biz_monthly: 1月は 50000 - 5000 = 45000
  assert.equal(v.biz_monthly.months[0], 45000);
  assert.equal(v.biz_monthly.total, 45000);
});

test("事業科目なしなら biz_monthly=null", () => {
  const v = composeMonthlyComparisonView(jsResult({
    income_accounts: [
      { code: "4010", name: "給与", months: months([[0, 100000]]), total: 100000 },
    ],
  }), {
    "4010": { type: "revenue", name: "給与", cost_type: "fixed", is_business: false },
  });
  assert.equal(v.biz_monthly, null);
});


// --- totals & breakdown ---

test("income_totals/expense_totals/net_totals は household のみで再計算", () => {
  const v = composeMonthlyComparisonView(jsResult({
    income_accounts: [
      { code: "4010", name: "給与", months: months([[5, 100000]]), total: 100000 },
    ],
    expense_accounts: [
      { code: "5010", name: "食費", months: months([[5, 30000]]), total: 30000 },
    ],
  }), {
    "4010": { type: "revenue", name: "給与", cost_type: "fixed", is_business: false },
    "5010": { type: "expense", name: "食費", cost_type: "variable", is_business: false },
  });
  assert.equal(v.income_totals[5], 100000);
  assert.equal(v.expense_totals[5], 30000);
  assert.equal(v.net_totals[5], 70000);
});

test("breakdown: cost_type 別に集計、grand 合計も", () => {
  const v = composeMonthlyComparisonView(jsResult({
    income_accounts: [
      { code: "4010", name: "給与", months: months([[0, 100000]]), total: 100000 },
      { code: "4020", name: "副業", months: months([[0, 50000]]), total: 50000 },
      { code: "4030", name: "贈与", months: months([[0, 20000]]), total: 20000 },
    ],
    expense_accounts: [
      { code: "5010", name: "家賃", months: months([[0, 80000]]), total: 80000 },
      { code: "5020", name: "食費", months: months([[0, 30000]]), total: 30000 },
      { code: "5030", name: "旅行", months: months([[0, 10000]]), total: 10000 },
    ],
  }), {
    "4010": { type: "revenue", cost_type: "fixed", is_business: false },
    "4020": { type: "revenue", cost_type: "variable", is_business: false },
    "4030": { type: "revenue", cost_type: "occasional", is_business: false },
    "5010": { type: "expense", cost_type: "fixed", is_business: false },
    "5020": { type: "expense", cost_type: "variable", is_business: false },
    "5030": { type: "expense", cost_type: "occasional", is_business: false },
  });
  assert.equal(v.breakdown.income_totals.fixed, 100000);
  assert.equal(v.breakdown.income_totals.variable, 50000);
  assert.equal(v.breakdown.income_totals.occasional, 20000);
  assert.equal(v.breakdown.income_grand, 170000);
  assert.equal(v.breakdown.expense_totals.fixed, 80000);
  assert.equal(v.breakdown.expense_totals.variable, 30000);
  assert.equal(v.breakdown.expense_totals.occasional, 10000);
  assert.equal(v.breakdown.expense_grand, 120000);
});


// --- cost_type fallback ---

test("accountsMeta に cost_type がない / 値が想定外なら occasional 扱い", () => {
  const v = composeMonthlyComparisonView(jsResult({
    income_accounts: [
      { code: "4010", name: "給与", months: months([[0, 100]]), total: 100 },
    ],
  }), {
    "4010": { type: "revenue", is_business: false },  // cost_type なし
  });
  assert.equal(v.income_accounts[0].cost_type, "occasional");
  assert.equal(v.breakdown.income_totals.occasional, 100);
});
