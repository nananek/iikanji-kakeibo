// Tests for the pure compareBalanceSheet helper.

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/balance_sheet_validator.mjs",
  import.meta.url,
);
const { compareBalanceSheet } = await import(M.href);


function emptyJsResult(extra = {}) {
  return {
    assets: [], liabilities: [], equities: [],
    total_assets: 0, total_liabilities: 0, total_equity: 0,
    net_income: 0, has_closing: true,  // 加算なしの状態
    ...extra,
  };
}


test("完全一致 (has_closing=true) で diffs 空", () => {
  const server = [
    { code: "1010", section: "assets", balance: 1000 },
    { code: "2010", section: "liabilities", balance: 300 },
    { code: "3020", section: "equities", balance: 700 },
  ];
  const totals = { assets: 1000, liabilities: 300, equity: 700, net_income: 0 };
  const js = emptyJsResult({
    assets: [{ account_code: "1010", balance: 1000 }],
    liabilities: [{ account_code: "2010", balance: 300 }],
    equities: [{ account_code: "3020", balance: 700 }],
    total_assets: 1000, total_liabilities: 300, total_equity: 700,
    has_closing: true,
  });
  assert.deepEqual(compareBalanceSheet(server, totals, js), []);
});

test("損益振替前 (has_closing=false) — サーバ純資産は net_income 加算済みを比較", () => {
  // サーバ: total_equity に net_income を加算した値を表示
  // クライアント: computeBalanceSheet は total_equity と net_income を別フィールドで返す
  // → validator が has_closing=false のとき jsResult.total_equity + net_income を比較値とする
  const server = [
    { code: "1010", section: "assets", balance: 1000 },
    { code: "3020", section: "equities", balance: 500 },
  ];
  // サーバ: equity 500 + net_income 200 = 700 を total に
  const totals = { assets: 1000, liabilities: 0, equity: 700, net_income: 200 };
  const js = emptyJsResult({
    assets: [{ account_code: "1010", balance: 1000 }],
    equities: [{ account_code: "3020", balance: 500 }],
    total_assets: 1000, total_liabilities: 0, total_equity: 500,
    net_income: 200, has_closing: false,
  });
  assert.deepEqual(compareBalanceSheet(server, totals, js), []);
});

test("総資産不一致は total_assets_mismatch", () => {
  const server = [{ code: "1010", section: "assets", balance: 1000 }];
  const totals = { assets: 1000, liabilities: 0, equity: 0, net_income: 0 };
  const js = emptyJsResult({
    assets: [{ account_code: "1010", balance: 999 }],
    total_assets: 999, has_closing: true,
  });
  const d = compareBalanceSheet(server, totals, js);
  assert.ok(d.some((x) => x.kind === "total_assets_mismatch"));
  assert.ok(d.some((x) => x.kind === "balance_mismatch"));
});

test("サーバにある B/S 行が JS にない → missing_in_client", () => {
  const server = [{ code: "1010", section: "assets", balance: 1000 }];
  const totals = { assets: 1000, liabilities: 0, equity: 0, net_income: 0 };
  const js = emptyJsResult({ has_closing: true });  // js は空
  const d = compareBalanceSheet(server, totals, js);
  assert.ok(d.some((x) => x.kind === "missing_in_client" && x.code === "1010"));
});

test("サーバがゼロ行 → missing_in_client に出さない", () => {
  const server = [{ code: "1010", section: "assets", balance: 0 }];
  const totals = { assets: 0, liabilities: 0, equity: 0, net_income: 0 };
  const js = emptyJsResult({ has_closing: true });
  // total_assets 一致 + ゼロ行 missing 抑制 → diffs 空
  assert.deepEqual(compareBalanceSheet(server, totals, js), []);
});

test("JS にあるがサーバにない → extra_in_client (非ゼロのみ)", () => {
  const server = [{ code: "1010", section: "assets", balance: 1000 }];
  const totals = { assets: 1000, liabilities: 0, equity: 0, net_income: 0 };
  const js = emptyJsResult({
    assets: [
      { account_code: "1010", balance: 1000 },
      { account_code: "1099", balance: 50 },  // 余分
    ],
    liabilities: [{ account_code: "2099", balance: 0 }],  // ゼロは無視
    total_assets: 1050, has_closing: true,
  });
  const d = compareBalanceSheet(server, totals, js);
  // total_assets_mismatch + extra_in_client (1099) は出るが、ゼロの 2099 は出ない
  assert.ok(d.some((x) => x.kind === "extra_in_client" && x.code === "1099"));
  assert.equal(d.filter((x) => x.kind === "extra_in_client").length, 1);
});

test("section が違うと別行として扱う", () => {
  // 同じ code が assets と liabilities にあるケース (本来は起きないが防御)
  const server = [
    { code: "1010", section: "assets", balance: 1000 },
    { code: "1010", section: "liabilities", balance: 50 },
  ];
  const totals = { assets: 1000, liabilities: 50, equity: 0, net_income: 0 };
  const js = emptyJsResult({
    assets: [{ account_code: "1010", balance: 1000 }],
    liabilities: [{ account_code: "1010", balance: 50 }],
    total_assets: 1000, total_liabilities: 50, has_closing: true,
  });
  assert.deepEqual(compareBalanceSheet(server, totals, js), []);
});

test("空配列同士 (帳簿ゼロ) は diffs 空", () => {
  const totals = { assets: 0, liabilities: 0, equity: 0, net_income: 0 };
  assert.deepEqual(compareBalanceSheet([], totals, emptyJsResult({ has_closing: true })), []);
});
