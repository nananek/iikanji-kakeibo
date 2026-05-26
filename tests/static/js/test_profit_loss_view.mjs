// Phase E3-F-3b: composeProfitLossView の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/profit_loss_view.js",
  import.meta.url,
);
const { composeProfitLossView } = await import(M.href);


// --- helper ---

function jsResult(income, expense, ib = [], eb = []) {
  return {
    income_total: income,
    expense_total: expense,
    net_income: income - expense,
    income_breakdown: ib,
    expense_breakdown: eb,
  };
}


// --- argument validation ---

test("jsResult が object でないと TypeError", () => {
  assert.throws(() => composeProfitLossView(null), /object/);
});

test("income_breakdown / expense_breakdown が配列でないと TypeError", () => {
  assert.throws(
    () => composeProfitLossView({ income_breakdown: null, expense_breakdown: [] }),
    /arrays/,
  );
});


// --- summary basics ---

test("biz_income なし: summary は jsResult の合計をそのまま", () => {
  const v = composeProfitLossView(jsResult(10000, 3000));
  assert.deepEqual(v.summary, { income: 10000, expense: 3000, balance: 7000 });
  assert.equal(v.bizIncome, null);
});

test("負の収支は balance がマイナスになる", () => {
  const v = composeProfitLossView(jsResult(1000, 5000));
  assert.equal(v.summary.balance, -4000);
});


// --- biz_income ---

test("biz_income has_mappings=true + income>0 で summary に合算", () => {
  const v = composeProfitLossView(
    jsResult(10000, 3000),
    { bizIncome: { has_mappings: true, income: 50000 } },
  );
  assert.equal(v.summary.income, 60000);
  assert.equal(v.summary.balance, 57000);
  assert.deepEqual(v.bizIncome, { income: 50000 });
});

test("biz_income has_mappings=false なら無視", () => {
  const v = composeProfitLossView(
    jsResult(10000, 3000),
    { bizIncome: { has_mappings: false, income: 50000 } },
  );
  assert.equal(v.summary.income, 10000);
  assert.equal(v.bizIncome, null);
});

test("biz_income income=0 なら無視", () => {
  const v = composeProfitLossView(
    jsResult(10000, 3000),
    { bizIncome: { has_mappings: true, income: 0 } },
  );
  assert.equal(v.summary.income, 10000);
  assert.equal(v.bizIncome, null);
});

test("biz_income null なら無視", () => {
  const v = composeProfitLossView(
    jsResult(10000, 3000), { bizIncome: null },
  );
  assert.equal(v.summary.income, 10000);
  assert.equal(v.bizIncome, null);
});


// --- breakdown passthrough (shallow copy) ---

test("breakdown は浅いコピーで透過", () => {
  const ib = [{ account_code: "4010", account_name: "売上", amount: 10000 }];
  const eb = [{ account_code: "5010", account_name: "消耗品費", amount: 3000 }];
  const v = composeProfitLossView(jsResult(10000, 3000, ib, eb));
  assert.deepEqual(v.income_breakdown, ib);
  assert.deepEqual(v.expense_breakdown, eb);
  // 元配列を変更しても view 側は影響を受けない (slice しているか確認)
  ib.push({ account_code: "9999" });
  assert.equal(v.income_breakdown.length, 1);
});
