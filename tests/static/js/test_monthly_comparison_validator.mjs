// Tests for the pure compareMonthlyComparison helper.
// _run() (DOM + dynamic imports) is browser-only and not covered here.

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/monthly_comparison_validator.mjs",
  import.meta.url,
);
const { compareMonthlyComparison } = await import(M.href);


function months(arr) {
  const out = new Array(12).fill(0);
  for (const [i, v] of arr) out[i] = v;
  return out;
}


test("完全一致で diffs 空", () => {
  const server = [
    { code: "4010", type: "income", months: months([[0, 100], [5, 200]]), total: 300 },
    { code: "5010", type: "expense", months: months([[2, 50]]), total: 50 },
  ];
  const js = {
    income_accounts: [{ code: "4010", months: months([[0, 100], [5, 200]]), total: 300 }],
    expense_accounts: [{ code: "5010", months: months([[2, 50]]), total: 50 }],
  };
  assert.deepEqual(compareMonthlyComparison(server, js), []);
});

test("月ベクトル不一致は mismatch", () => {
  const server = [{ code: "4010", type: "income", months: months([[0, 100]]), total: 100 }];
  const js = {
    income_accounts: [{ code: "4010", months: months([[0, 99]]), total: 99 }],
    expense_accounts: [],
  };
  const d = compareMonthlyComparison(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "mismatch");
  assert.equal(d[0].code, "4010");
  assert.equal(d[0].server.total, 100);
  assert.equal(d[0].client.total, 99);
});

test("total 一致でも month ベクトルがズレていれば mismatch", () => {
  // 同じ年間 total だが月配分が違う = 集計ロジックのバグを検知できる
  const server = [{ code: "4010", type: "income", months: months([[0, 100]]), total: 100 }];
  const js = {
    income_accounts: [{ code: "4010", months: months([[6, 100]]), total: 100 }],
    expense_accounts: [],
  };
  const d = compareMonthlyComparison(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "mismatch");
});

test("type 不一致 (income/expense) は mismatch", () => {
  const server = [{ code: "4010", type: "income", months: months([[0, 100]]), total: 100 }];
  const js = {
    income_accounts: [],
    expense_accounts: [{ code: "4010", months: months([[0, 100]]), total: 100 }],
  };
  const d = compareMonthlyComparison(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "mismatch");
});

test("サーバにあるが JS にない → missing_in_client", () => {
  const server = [{ code: "4010", type: "income", months: months([[0, 100]]), total: 100 }];
  const js = { income_accounts: [], expense_accounts: [] };
  const d = compareMonthlyComparison(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "missing_in_client");
});

test("サーバ側がゼロベクトル → missing_in_client に出さない", () => {
  const server = [
    { code: "4010", type: "income", months: new Array(12).fill(0), total: 0 },
  ];
  const js = { income_accounts: [], expense_accounts: [] };
  assert.deepEqual(compareMonthlyComparison(server, js), []);
});

test("JS にあるがサーバにない → extra_in_client (非ゼロのみ)", () => {
  const server = [{ code: "4010", type: "income", months: months([[0, 100]]), total: 100 }];
  const js = {
    income_accounts: [
      { code: "4010", months: months([[0, 100]]), total: 100 },
      { code: "4099", months: months([[3, 50]]), total: 50 },  // 余分
    ],
    expense_accounts: [
      { code: "5099", months: new Array(12).fill(0), total: 0 },  // ゼロは無視
    ],
  };
  const d = compareMonthlyComparison(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "extra_in_client");
  assert.equal(d[0].code, "4099");
});

test("空配列同士は diffs 空", () => {
  assert.deepEqual(
    compareMonthlyComparison([], { income_accounts: [], expense_accounts: [] }),
    [],
  );
});
