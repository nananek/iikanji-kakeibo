// Tests for the pure compareProfitLoss helper.
// _run() (DOM + dynamic imports) is browser-only and not covered here.

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/profit_loss_validator.mjs",
  import.meta.url,
);
const { compareProfitLoss } = await import(M.href);


test("compareProfitLoss: 完全一致で diffs 空", () => {
  const server = [
    { code: "4010", type: "income", amount: 1000 },
    { code: "5010", type: "expense", amount: 400 },
  ];
  const js = {
    income_breakdown: [{ account_code: "4010", amount: 1000 }],
    expense_breakdown: [{ account_code: "5010", amount: 400 }],
  };
  assert.deepEqual(compareProfitLoss(server, js), []);
});

test("compareProfitLoss: 金額不一致は mismatch", () => {
  const server = [{ code: "4010", type: "income", amount: 1000 }];
  const js = {
    income_breakdown: [{ account_code: "4010", amount: 999 }],
    expense_breakdown: [],
  };
  const d = compareProfitLoss(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "mismatch");
  assert.equal(d[0].code, "4010");
  assert.equal(d[0].server.amount, 1000);
  assert.equal(d[0].client.amount, 999);
});

test("compareProfitLoss: type 不一致も mismatch", () => {
  // 同じ code で server は income / client は expense — マッピングミスを検知
  const server = [{ code: "4010", type: "income", amount: 1000 }];
  const js = {
    income_breakdown: [],
    expense_breakdown: [{ account_code: "4010", amount: 1000 }],
  };
  const d = compareProfitLoss(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "mismatch");
});

test("compareProfitLoss: サーバにあるが JS にない → missing_in_client", () => {
  const server = [{ code: "5010", type: "expense", amount: 500 }];
  const js = { income_breakdown: [], expense_breakdown: [] };
  const d = compareProfitLoss(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "missing_in_client");
  assert.equal(d[0].code, "5010");
});

test("compareProfitLoss: サーバがゼロ行 → missing_in_client に出さない", () => {
  // computeProfitLoss は amount==0 を breakdown から除外する仕様のため、
  // サーバが 0 円の科目を出していても missing 扱いしない。
  const server = [{ code: "5010", type: "expense", amount: 0 }];
  const js = { income_breakdown: [], expense_breakdown: [] };
  assert.deepEqual(compareProfitLoss(server, js), []);
});

test("compareProfitLoss: JS にあるがサーバにない → extra_in_client (非ゼロのみ)", () => {
  const server = [{ code: "4010", type: "income", amount: 1000 }];
  const js = {
    income_breakdown: [
      { account_code: "4010", amount: 1000 },
      { account_code: "4099", amount: 50 },  // 余分
    ],
    expense_breakdown: [
      { account_code: "5099", amount: 0 },   // ゼロは無視
    ],
  };
  const d = compareProfitLoss(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "extra_in_client");
  assert.equal(d[0].code, "4099");
});

test("compareProfitLoss: 空配列同士は diffs 空", () => {
  assert.deepEqual(
    compareProfitLoss([], { income_breakdown: [], expense_breakdown: [] }),
    [],
  );
});
