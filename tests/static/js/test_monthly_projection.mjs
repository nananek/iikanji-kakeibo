// Phase E3-F-4e: computeProjection / collectDailyAmounts28d の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/monthly_projection.js",
  import.meta.url,
);
const { computeProjection, collectDailyAmounts28d } = await import(M.href);


// --- helpers ---

const META = {
  "4010": { type: "revenue", cost_type: "variable" },
  "4020": { type: "revenue", cost_type: "fixed" },
  "5010": { type: "expense", cost_type: "variable" },
  "5020": { type: "expense", cost_type: "fixed" },
  "5030": { type: "expense", cost_type: "occasional" },
};

function emptyMonths() {
  return Array(12).fill(0);
}

function makeView(opts = {}) {
  return {
    income_accounts: opts.income_accounts || [],
    expense_accounts: opts.expense_accounts || [],
    income_totals: opts.income_totals || emptyMonths(),
    expense_totals: opts.expense_totals || emptyMonths(),
    net_totals: emptyMonths(),
    biz_monthly: null,
    breakdown: {},
  };
}

function entry(date, lines, source) {
  return { id: 1, fiscal_year: 2026, fiscal_period: 5, source: source || "journal", date, lines };
}


// --- arg validation ---

test("view が object でないと TypeError", () => {
  assert.throws(() => computeProjection(null, [], { accountsMeta: META, year: 2026, month: 5 }), /object/);
});

test("accountsMeta なしで TypeError", () => {
  assert.throws(() => computeProjection(makeView(), [], { year: 2026, month: 5 }), /accountsMeta/);
});

test("year/month 不正で TypeError", () => {
  assert.throws(() => computeProjection(makeView(), [], { accountsMeta: META, year: 2026, month: 13 }), /year.*month/);
});


// --- pro_rata ---

test("pro_rata: variable 科目は actual * days_in_month / days_elapsed", () => {
  // 5月 (31日)、15日経過、actual=15000 → projected = 15000 * 31 / 15 = 31000
  const view = makeView({
    expense_accounts: [{
      code: "5010", name: "食費", cost_type: "variable",
      months: (() => { const a = emptyMonths(); a[4] = 15000; return a; })(),
      total: 15000,
    }],
    expense_totals: (() => { const a = emptyMonths(); a[4] = 15000; return a; })(),
  });
  const today = new Date(Date.UTC(2026, 4, 15));
  const r = computeProjection(view, [], {
    method: "pro_rata", year: 2026, month: 5, today, accountsMeta: META,
  });
  assert.equal(r.days_in_month, 31);
  assert.equal(r.days_elapsed, 15);
  assert.equal(r.expense_projected[0].projected, 31000);
});

test("fixed 科目: 前月 actual を採用 (前月 0 なら当月 actual)", () => {
  const months = emptyMonths();
  months[3] = 80000;  // 4月の固定
  months[4] = 80000;  // 5月の固定 (既に発生)
  const view = makeView({
    expense_accounts: [{
      code: "5020", name: "家賃", cost_type: "fixed",
      months, total: 160000,
    }],
    expense_totals: months,
  });
  const r = computeProjection(view, [], {
    method: "pro_rata", year: 2026, month: 5,
    today: new Date(Date.UTC(2026, 4, 15)),
    accountsMeta: META,
  });
  assert.equal(r.expense_projected[0].projected, 80000);  // 前月 = 当月予想
});

test("occasional 科目: 実績そのまま", () => {
  const months = emptyMonths();
  months[4] = 5000;
  const view = makeView({
    expense_accounts: [{
      code: "5030", name: "旅行", cost_type: "occasional",
      months, total: 5000,
    }],
    expense_totals: months,
  });
  const r = computeProjection(view, [], {
    method: "pro_rata", year: 2026, month: 5,
    today: new Date(Date.UTC(2026, 4, 15)),
    accountsMeta: META,
  });
  assert.equal(r.expense_projected[0].projected, 5000);
});


// --- rolling28 ---

test("rolling28: 過去 28 日 daily 平均 × 残り日数 + 当月 actual", () => {
  // 5/15 視点、過去 28 日 (4/17..5/14) に 5010 daily=1000 を 28 日連続
  // total_28d=28000, daily_avg=1000, remaining_days=31-15=16
  // projected = 15000 (actual) + 1000 * 16 = 31000
  const entries = [];
  for (let i = 0; i < 28; i++) {
    const ms = Date.UTC(2026, 4, 14) - i * 86400000;
    const d = new Date(ms);
    const ds = d.getUTCFullYear() + "-"
      + String(d.getUTCMonth() + 1).padStart(2, "0") + "-"
      + String(d.getUTCDate()).padStart(2, "0");
    entries.push(entry(ds, [
      { account_code: "5010", debit: 1000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 1000 },
    ]));
  }
  const months = emptyMonths();
  months[4] = 15000;
  const view = makeView({
    expense_accounts: [{
      code: "5010", name: "食費", cost_type: "variable",
      months, total: 15000,
    }],
    expense_totals: months,
  });
  const r = computeProjection(view, entries, {
    method: "rolling28", year: 2026, month: 5,
    today: new Date(Date.UTC(2026, 4, 15)),
    accountsMeta: META,
  });
  assert.equal(r.expense_projected[0].projected, 31000);
});

test("rolling28: daily データなしなら pro_rata fallback", () => {
  const months = emptyMonths();
  months[4] = 15000;
  const view = makeView({
    expense_accounts: [{
      code: "5010", name: "食費", cost_type: "variable",
      months, total: 15000,
    }],
    expense_totals: months,
  });
  const r = computeProjection(view, [], {
    method: "rolling28", year: 2026, month: 5,
    today: new Date(Date.UTC(2026, 4, 15)),
    accountsMeta: META,
  });
  // pro_rata と同じ: 31000
  assert.equal(r.expense_projected[0].projected, 31000);
});


// --- dow28 ---

test("dow28: 28 日均一データなら rolling28 と同値", () => {
  // 5/15 視点、過去 28 日に毎日 1000 → 各曜日 4 日 × 1000 = 4000、
  // dow_avg=1000、remaining 16 日 → 16000、projected=actual+16000=31000
  const entries = [];
  for (let i = 0; i < 28; i++) {
    const ms = Date.UTC(2026, 4, 14) - i * 86400000;
    const d = new Date(ms);
    const ds = d.getUTCFullYear() + "-"
      + String(d.getUTCMonth() + 1).padStart(2, "0") + "-"
      + String(d.getUTCDate()).padStart(2, "0");
    entries.push(entry(ds, [
      { account_code: "5010", debit: 1000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 1000 },
    ]));
  }
  const months = emptyMonths();
  months[4] = 15000;
  const view = makeView({
    expense_accounts: [{
      code: "5010", name: "食費", cost_type: "variable",
      months, total: 15000,
    }],
    expense_totals: months,
  });
  const r = computeProjection(view, entries, {
    method: "dow28", year: 2026, month: 5,
    today: new Date(Date.UTC(2026, 4, 15)),
    accountsMeta: META,
  });
  assert.equal(r.expense_projected[0].projected, 31000);
});

test("dow28: 曜日別データで曜日が反映される", () => {
  // 月曜のみ 7000、それ以外 0 を 28 日分。
  // 過去 28 日 (4/17..5/14) に月曜は 4 回 → dow_avg[月曜] = 28000/4 = 7000
  // 5/15 視点で 5/16..5/31 (= 16 日) のうち月曜は何回?
  // 5/16=土, 5/17=日, 5/18=月, 5/25=月 → 月曜 = 2 回 → remaining_sum = 14000
  // actual=0, projected = 0 + 14000 = 14000
  const entries = [];
  for (let i = 0; i < 28; i++) {
    const ms = Date.UTC(2026, 4, 14) - i * 86400000;
    const d = new Date(ms);
    if (d.getUTCDay() !== 1) continue;  // 月曜のみ
    const ds = d.getUTCFullYear() + "-"
      + String(d.getUTCMonth() + 1).padStart(2, "0") + "-"
      + String(d.getUTCDate()).padStart(2, "0");
    entries.push(entry(ds, [
      { account_code: "5010", debit: 7000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 7000 },
    ]));
  }
  const months = emptyMonths();
  const view = makeView({
    expense_accounts: [{
      code: "5010", name: "食費", cost_type: "variable",
      months, total: 0,
    }],
    expense_totals: months,
  });
  const r = computeProjection(view, entries, {
    method: "dow28", year: 2026, month: 5,
    today: new Date(Date.UTC(2026, 4, 15)),
    accountsMeta: META,
  });
  assert.equal(r.expense_projected[0].projected, 14000);
});

test("dow28: daily データなしなら pro_rata fallback", () => {
  const months = emptyMonths();
  months[4] = 15000;
  const view = makeView({
    expense_accounts: [{
      code: "5010", name: "食費", cost_type: "variable",
      months, total: 15000,
    }],
    expense_totals: months,
  });
  const r = computeProjection(view, [], {
    method: "dow28", year: 2026, month: 5,
    today: new Date(Date.UTC(2026, 4, 15)),
    accountsMeta: META,
  });
  // pro_rata と同じ: 31000
  assert.equal(r.expense_projected[0].projected, 31000);
});


// --- method label / totals ---

test("総合計 income_total_projected / expense_total_projected を返す", () => {
  const months = emptyMonths();
  months[4] = 10000;
  const view = makeView({
    income_accounts: [{
      code: "4010", name: "売上", cost_type: "variable",
      months, total: 10000,
    }],
    income_totals: months,
  });
  const r = computeProjection(view, [], {
    method: "pro_rata", year: 2026, month: 5,
    today: new Date(Date.UTC(2026, 4, 15)),
    accountsMeta: META,
  });
  assert.equal(r.income_total_actual, 10000);
  assert.equal(r.income_total_projected, r.income_projected[0].projected);
});

test("不正な method は pro_rata に fallback", () => {
  const months = emptyMonths();
  months[4] = 10;
  const view = makeView({
    expense_accounts: [{ code: "5010", cost_type: "variable", months, total: 10 }],
    expense_totals: months,
  });
  const r = computeProjection(view, [], {
    method: "unknown", year: 2026, month: 5,
    today: new Date(Date.UTC(2026, 4, 15)),
    accountsMeta: META,
  });
  assert.equal(r.method, "pro_rata");
});


// --- collectDailyAmounts28d ---

test("collectDailyAmounts28d: variable 科目のみ集計", () => {
  const entries = [
    entry("2026-05-10", [{ account_code: "5010", debit: 500, credit: 0 }]),
    entry("2026-05-10", [{ account_code: "5020", debit: 999, credit: 0 }]),  // fixed → 除外
    entry("2026-05-10", [{ account_code: "5030", debit: 999, credit: 0 }]),  // occasional → 除外
  ];
  const daily = collectDailyAmounts28d(entries, META, {
    referenceDate: new Date(Date.UTC(2026, 4, 14)),
  });
  assert.deepEqual(daily, { "5010": { "2026-05-10": 500 } });
});

test("collectDailyAmounts28d: 28 日範囲外は無視", () => {
  // refDate 2026-05-14 → 4/17..5/14 が範囲
  const entries = [
    entry("2026-04-16", [{ account_code: "5010", debit: 100, credit: 0 }]),  // 範囲外
    entry("2026-04-17", [{ account_code: "5010", debit: 100, credit: 0 }]),  // 境界
    entry("2026-05-15", [{ account_code: "5010", debit: 100, credit: 0 }]),  // 範囲外
  ];
  const daily = collectDailyAmounts28d(entries, META, {
    referenceDate: new Date(Date.UTC(2026, 4, 14)),
  });
  assert.deepEqual(daily, { "5010": { "2026-04-17": 100 } });
});

test("collectDailyAmounts28d: closing 仕訳除外", () => {
  const daily = collectDailyAmounts28d([
    entry("2026-05-10", [{ account_code: "5010", debit: 500, credit: 0 }], "closing"),
  ], META, {
    referenceDate: new Date(Date.UTC(2026, 4, 14)),
  });
  assert.deepEqual(daily, {});
});
