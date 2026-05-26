// Phase E3-F-3c: composeBalanceSheetView の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/balance_sheet_view.js",
  import.meta.url,
);
const { composeBalanceSheetView } = await import(M.href);


// --- helper ---

function bsResult({
  assets = [], liabilities = [], equities = [],
  total_assets = 0, total_liabilities = 0, total_equity = 0,
  net_income = 0, has_closing = false,
} = {}) {
  return {
    assets, liabilities, equities,
    total_assets, total_liabilities, total_equity,
    net_income, has_closing,
  };
}


// --- arg validation ---

test("jsResult が object でないと TypeError", () => {
  assert.throws(() => composeBalanceSheetView(null), /object/);
});

test("assets/liabilities/equities が配列でないと TypeError", () => {
  assert.throws(
    () => composeBalanceSheetView({ assets: null }),
    /array/,
  );
});


// --- balanced cases ---

test("資産=負債+純資産で is_balanced=true", () => {
  const v = composeBalanceSheetView(bsResult({
    assets: [{ account_code: "1010", account_name: "現金", balance: 10000 }],
    liabilities: [{ account_code: "2010", account_name: "未払金", balance: 3000 }],
    equities: [{ account_code: "3010", account_name: "元入金", balance: 7000 }],
    total_assets: 10000, total_liabilities: 3000, total_equity: 7000,
    has_closing: true,
  }));
  assert.equal(v.totals.is_balanced, true);
  assert.equal(v.totals.diff, 0);
});

test("資産≠負債+純資産で is_balanced=false + diff", () => {
  const v = composeBalanceSheetView(bsResult({
    total_assets: 10000, total_liabilities: 3000, total_equity: 5000,
    has_closing: true,
  }));
  assert.equal(v.totals.is_balanced, false);
  assert.equal(v.totals.diff, 2000);
});


// --- net income merge ---

test("損益振替前 (has_closing=false) は equity に net_income を加算", () => {
  const v = composeBalanceSheetView(bsResult({
    total_assets: 10000, total_liabilities: 3000, total_equity: 5000,
    net_income: 2000, has_closing: false,
  }));
  assert.equal(v.totals.equity_with_ni, 7000);
  assert.equal(v.totals.is_balanced, true);
  assert.deepEqual(v.sections.equities.net_income_row, { balance: 2000 });
});

test("損益振替後 (has_closing=true) は net_income_row=null", () => {
  const v = composeBalanceSheetView(bsResult({
    total_equity: 7000, net_income: 0, has_closing: true,
  }));
  assert.equal(v.sections.equities.net_income_row, null);
});

test("net_income=0 なら net_income_row=null (損益振替前でも)", () => {
  const v = composeBalanceSheetView(bsResult({
    net_income: 0, has_closing: false,
  }));
  assert.equal(v.sections.equities.net_income_row, null);
});


// --- section totals ---

test("sections.totals が jsResult のサマリーを反映", () => {
  const v = composeBalanceSheetView(bsResult({
    total_assets: 100, total_liabilities: 30, total_equity: 70,
    has_closing: true,
  }));
  assert.equal(v.sections.assets.total, 100);
  assert.equal(v.sections.liabilities.total, 30);
  // 純資産は equity_with_ni
  assert.equal(v.sections.equities.total, 70);
});


// --- row passthrough ---

test("各セクションの rows はコピーで透過 (元配列変更の影響を受けない)", () => {
  const assets = [{ account_code: "1010", account_name: "現金", balance: 1000 }];
  const v = composeBalanceSheetView(bsResult({
    assets, total_assets: 1000, has_closing: true,
  }));
  assert.equal(v.sections.assets.rows.length, 1);
  assets.push({ account_code: "9999" });
  assert.equal(v.sections.assets.rows.length, 1);
});
