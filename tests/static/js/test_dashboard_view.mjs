// Phase E3-F-4d: composeDashboardView の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/dashboard_view.js",
  import.meta.url,
);
const { composeDashboardView } = await import(M.href);


// --- helpers ---

function entry(opts) {
  return {
    id: opts.id ?? 1,
    fiscal_year: opts.fiscal_year ?? 2026,
    fiscal_period: opts.fiscal_period ?? 5,
    source: opts.source || "journal",
    date: opts.date || "2026-05-15",
    description: opts.description || "",
    lines: opts.lines || [],
  };
}

const META = {
  "4010": { type: "revenue" },
  "5010": { type: "expense" },
  "1010": { type: "asset" },
  "9010": { type: "revenue" },
};


// --- arg validation ---

test("entries が配列でないと TypeError", () => {
  assert.throws(() => composeDashboardView(null, META), /array/);
});

test("accountsMeta が object でないと TypeError", () => {
  assert.throws(() => composeDashboardView([], null), /object/);
});


// --- empty ---

test("空 entries で全て 0", () => {
  const v = composeDashboardView([], META, { month: 5, untilMonth: 5 });
  assert.deepEqual(v.monthly, { income: 0, expense: 0, balance: 0 });
  assert.deepEqual(v.yearly, { income: 0, expense: 0, balance: 0 });
  assert.equal(v.monthly_trend.length, 5);
});


// --- monthly aggregation ---

test("当月の収益/費用が monthly に集計", () => {
  const v = composeDashboardView([
    entry({
      fiscal_period: 5,
      lines: [
        { account_code: "1010", debit: 100000, credit: 0 },
        { account_code: "4010", debit: 0, credit: 100000 },
      ],
    }),
    entry({
      fiscal_period: 5,
      lines: [
        { account_code: "5010", debit: 3000, credit: 0 },
        { account_code: "1010", debit: 0, credit: 3000 },
      ],
    }),
  ], META, { month: 5 });
  assert.equal(v.monthly.income, 100000);
  assert.equal(v.monthly.expense, 3000);
  assert.equal(v.monthly.balance, 97000);
});

test("他月の仕訳は monthly に含まれない", () => {
  const v = composeDashboardView([
    entry({
      fiscal_period: 3,
      lines: [
        { account_code: "5010", debit: 5000, credit: 0 },
        { account_code: "1010", debit: 0, credit: 5000 },
      ],
    }),
  ], META, { month: 5 });
  assert.equal(v.monthly.expense, 0);
});


// --- yearly aggregation ---

test("yearly は全期間 (fp=0..15) を含み closing を除外", () => {
  const v = composeDashboardView([
    entry({
      fiscal_period: 3,
      lines: [
        { account_code: "1010", debit: 100, credit: 0 },
        { account_code: "4010", debit: 0, credit: 100 },
      ],
    }),
    entry({
      fiscal_period: 13,  // 決算整理
      lines: [
        { account_code: "5010", debit: 50, credit: 0 },
        { account_code: "1010", debit: 0, credit: 50 },
      ],
    }),
    entry({
      fiscal_period: 16,  // 損益振替 → 除外
      lines: [
        { account_code: "5010", debit: 99999, credit: 0 },
      ],
    }),
    entry({
      fiscal_period: 5, source: "closing",  // closing → 除外
      lines: [
        { account_code: "5010", debit: 88888, credit: 0 },
      ],
    }),
  ], META, { month: 12 });
  assert.equal(v.yearly.income, 100);
  assert.equal(v.yearly.expense, 50);
  assert.equal(v.yearly.balance, 50);
});


// --- monthly_trend ---

test("monthly_trend は 1..untilMonth の配列", () => {
  const v = composeDashboardView([
    entry({
      fiscal_period: 1,
      lines: [
        { account_code: "4010", debit: 0, credit: 1000 },
      ],
    }),
    entry({
      fiscal_period: 2,
      lines: [
        { account_code: "5010", debit: 200, credit: 0 },
      ],
    }),
    entry({
      fiscal_period: 5,
      lines: [
        { account_code: "5010", debit: 999, credit: 0 },
      ],
    }),
  ], META, { month: 3, untilMonth: 3 });
  assert.equal(v.monthly_trend.length, 3);
  assert.deepEqual(v.monthly_trend[0], { month: 1, income: 1000, expense: 0 });
  assert.deepEqual(v.monthly_trend[1], { month: 2, income: 0, expense: 200 });
  assert.deepEqual(v.monthly_trend[2], { month: 3, income: 0, expense: 0 });
});

test("untilMonth=0 で monthly_trend=[]", () => {
  const v = composeDashboardView([], META, { month: 5, untilMonth: 0 });
  assert.equal(v.monthly_trend.length, 0);
});


// --- accountsMeta の影響 ---

test("accountsMeta にない code は無視される", () => {
  const v = composeDashboardView([
    entry({
      fiscal_period: 5,
      lines: [
        { account_code: "9999", debit: 0, credit: 1000 },
      ],
    }),
  ], META, { month: 5 });
  assert.equal(v.monthly.income, 0);
  assert.equal(v.monthly.expense, 0);
});

test("revenue/expense 以外の type は monthly/yearly に含まれない", () => {
  const v = composeDashboardView([
    entry({
      fiscal_period: 5,
      lines: [
        { account_code: "1010", debit: 100000, credit: 0 },  // asset
      ],
    }),
  ], META, { month: 5 });
  assert.equal(v.monthly.income, 0);
  assert.equal(v.monthly.expense, 0);
});
