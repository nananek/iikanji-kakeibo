// monthly_comparison.js (Phase E3-C-5) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/monthly_comparison.js",
  import.meta.url,
);
const { computeMonthlyComparison } = await import(M.href);


const TYPES = {
  "4010": "revenue", "4020": "revenue",
  "5010": "expense", "5020": "expense",
  "1010": "asset", "3010": "equity",
};
const NAMES = {
  "4010": "売上", "4020": "雑収入", "5010": "食費", "5020": "消耗品",
};

function entry(id, fp, source, lines) {
  return {
    id, fiscal_period: fp, source,
    lines: lines.map(([code, debit, credit]) => ({
      account_code: code, debit, credit,
    })),
  };
}


test("空配列で全 0", () => {
  const r = computeMonthlyComparison([], { accountTypeByCode: TYPES });
  assert.deepEqual(r.expense_accounts, []);
  assert.deepEqual(r.income_accounts, []);
  assert.deepEqual(r.expense_totals, Array(12).fill(0));
  assert.deepEqual(r.income_totals, Array(12).fill(0));
  assert.deepEqual(r.net_totals, Array(12).fill(0));
});

test("options 不足で TypeError", () => {
  assert.throws(() => computeMonthlyComparison([], {}), /accountTypeByCode/);
  assert.throws(
    () => computeMonthlyComparison(null, { accountTypeByCode: {} }),
    /array/,
  );
});

test("各月の収益・費用が pivot される", () => {
  const entries = [
    entry(1, 1, "journal", [["5010", 100, 0], ["1010", 0, 100]]),  // 1 月食費
    entry(2, 3, "journal", [["5010", 200, 0], ["1010", 0, 200]]),  // 3 月食費
    entry(3, 1, "journal", [["1010", 1000, 0], ["4010", 0, 1000]]),  // 1 月売上
  ];
  const r = computeMonthlyComparison(entries, {
    accountTypeByCode: TYPES, accountNameByCode: NAMES,
  });
  assert.equal(r.expense_accounts.length, 1);
  assert.equal(r.expense_accounts[0].code, "5010");
  assert.equal(r.expense_accounts[0].name, "食費");
  assert.equal(r.expense_accounts[0].months[0], 100);  // 1 月
  assert.equal(r.expense_accounts[0].months[2], 200);  // 3 月
  assert.equal(r.expense_accounts[0].total, 300);
  assert.equal(r.income_accounts[0].months[0], 1000);
  assert.equal(r.income_accounts[0].total, 1000);

  assert.equal(r.expense_totals[0], 100);
  assert.equal(r.expense_totals[2], 200);
  assert.equal(r.income_totals[0], 1000);
  assert.equal(r.net_totals[0], 900);   // 1000 - 100
  assert.equal(r.net_totals[2], -200);
});

test("複数科目の合計集計", () => {
  const entries = [
    entry(1, 5, "journal", [
      ["5010", 100, 0], ["5020", 50, 0], ["1010", 0, 150],
    ]),
  ];
  const r = computeMonthlyComparison(entries, { accountTypeByCode: TYPES });
  assert.equal(r.expense_accounts.length, 2);
  assert.equal(r.expense_totals[4], 150);  // 5 月
});

test("fp=0 (期首) と fp=13-15 (決算整理) と fp=16 (損益振替) は除外", () => {
  const entries = [
    entry(1, 0, "journal", [["5010", 100, 0]]),    // 期首 — 除外
    entry(2, 13, "journal", [["5010", 200, 0]]),   // 決算整理 1 — 除外
    entry(3, 14, "journal", [["5010", 300, 0]]),   // 決算整理 2 — 除外
    entry(4, 15, "journal", [["5010", 400, 0]]),   // 決算整理 3 — 除外
    entry(5, 16, "journal", [["5010", 500, 0]]),   // 損益振替 — 除外
    entry(6, 5, "journal", [["5010", 999, 0]]),    // 5 月 — 集計
  ];
  const r = computeMonthlyComparison(entries, { accountTypeByCode: TYPES });
  // 5月 (idx 4) のみ
  assert.equal(r.expense_totals[4], 999);
  assert.equal(r.expense_totals.reduce((s, v) => s + v, 0), 999);
});

test("source=closing は除外", () => {
  const entries = [
    entry(1, 5, "journal", [["5010", 100, 0]]),
    entry(2, 5, "closing", [["5010", 200, 0]]),    // 除外
  ];
  const r = computeMonthlyComparison(entries, { accountTypeByCode: TYPES });
  assert.equal(r.expense_totals[4], 100);
});

test("fiscal_period が number でない (null/undefined) は除外", () => {
  const entries = [
    {id: 1, fiscal_period: null, source: "journal",
     lines: [{account_code: "5010", debit: 100, credit: 0}]},
    {id: 2, fiscal_period: undefined, source: "journal",
     lines: [{account_code: "5010", debit: 200, credit: 0}]},
    entry(3, 5, "journal", [["5010", 300, 0]]),
  ];
  const r = computeMonthlyComparison(entries, { accountTypeByCode: TYPES });
  assert.equal(r.expense_totals.reduce((s, v) => s + v, 0), 300);
});

test("収益の返金 (debit) で月内 net が減算される", () => {
  const entries = [
    entry(1, 5, "journal", [["1010", 1000, 0], ["4010", 0, 1000]]),  // 売上
    entry(2, 5, "journal", [["4010", 300, 0], ["1010", 0, 300]]),    // 返金
  ];
  const r = computeMonthlyComparison(entries, { accountTypeByCode: TYPES });
  assert.equal(r.income_accounts[0].months[4], 700);   // 1000 - 300
  assert.equal(r.income_totals[4], 700);
});

test("BS 科目 (asset/equity) は集計から除外", () => {
  const entries = [
    entry(1, 5, "journal", [["1010", 1000, 0], ["3010", 0, 1000]]),
  ];
  const r = computeMonthlyComparison(entries, { accountTypeByCode: TYPES });
  assert.deepEqual(r.expense_totals, Array(12).fill(0));
  assert.deepEqual(r.income_totals, Array(12).fill(0));
});

test("account_code null / unknown はスキップ", () => {
  const entries = [
    {id: 1, fiscal_period: 5, source: "journal", lines: [
      {account_code: null, debit: 100, credit: 0},
      {account_code: "UNKNOWN", debit: 200, credit: 0},
      {account_code: "5010", debit: 300, credit: 0},
    ]},
  ];
  const r = computeMonthlyComparison(entries, { accountTypeByCode: TYPES });
  assert.equal(r.expense_totals[4], 300);
});

test("12 ヶ月全月にデータがある場合", () => {
  const entries = [];
  for (let m = 1; m <= 12; m++) {
    entries.push(entry(m, m, "journal",
      [["5010", m * 100, 0], ["1010", 0, m * 100]]));
  }
  const r = computeMonthlyComparison(entries, { accountTypeByCode: TYPES });
  for (let i = 0; i < 12; i++) {
    assert.equal(r.expense_totals[i], (i + 1) * 100);
  }
  // total = 100 + 200 + ... + 1200 = 7800
  assert.equal(r.expense_accounts[0].total, 7800);
});

test("accounts のソート: code 昇順", () => {
  const entries = [
    entry(1, 5, "journal", [
      ["5020", 50, 0], ["5010", 100, 0], ["1010", 0, 150],
    ]),
  ];
  const r = computeMonthlyComparison(entries, { accountTypeByCode: TYPES });
  assert.deepEqual(r.expense_accounts.map(a => a.code), ["5010", "5020"]);
});
