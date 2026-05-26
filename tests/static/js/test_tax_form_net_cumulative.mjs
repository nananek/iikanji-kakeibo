// Phase #221: tax_form_renderer.mjs:_netCumulative の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/tax_form_renderer.mjs",
  import.meta.url,
);
const { _netCumulative } = await import(M.href);


// --- helpers ---

const META = {
  "1010": { type: "asset", normal_balance: "debit", name: "現金" },
  "2010": { type: "liability", normal_balance: "credit", name: "未払金" },
  "3010": { type: "equity", normal_balance: "credit", name: "元入金" },
  "4010": { type: "revenue", normal_balance: "credit", name: "売上" },
  "5010": { type: "expense", normal_balance: "debit", name: "食費" },
};


// --- basic netting ---

test("debit-normal 科目: d - c", () => {
  const r = _netCumulative({ "1010": [100, 30] }, META);
  assert.deepEqual(r, { "1010": 70 });
});

test("credit-normal 科目: c - d", () => {
  const r = _netCumulative({ "2010": [20, 80] }, META);
  assert.deepEqual(r, { "2010": 60 });
});


// --- BS-only filter (Issue #221 P/L 混入対策) ---

test("revenue 科目 (P/L) は除外", () => {
  const r = _netCumulative({
    "4010": [0, 1000000],
    "1010": [500, 0],
  }, META);
  // revenue は無視、asset のみ
  assert.deepEqual(r, { "1010": 500 });
});

test("expense 科目 (P/L) も除外", () => {
  const r = _netCumulative({
    "5010": [100000, 0],
    "2010": [0, 200],
  }, META);
  assert.deepEqual(r, { "2010": 200 });
});

test("asset/liability/equity 全 BS 科目を保持", () => {
  const r = _netCumulative({
    "1010": [100, 0],
    "2010": [0, 50],
    "3010": [0, 200],
  }, META);
  assert.deepEqual(r, { "1010": 100, "2010": 50, "3010": 200 });
});


// --- skip patterns ---

test("accountsMeta に存在しないコードは除外", () => {
  const r = _netCumulative({ "9999": [100, 0] }, META);
  assert.deepEqual(r, {});
});

test("pair が配列でない値は skip", () => {
  const r = _netCumulative({
    "1010": null,
    "2010": "invalid",
    "3010": 12345,
  }, META);
  assert.deepEqual(r, {});
});

test("pair の長さが 2 未満なら skip", () => {
  const r = _netCumulative({
    "1010": [50],
    "2010": [],
  }, META);
  assert.deepEqual(r, {});
});


// --- nullish ---

test("cumulative=null で空 dict を返す", () => {
  assert.deepEqual(_netCumulative(null, META), {});
});

test("cumulative=undefined で空 dict を返す", () => {
  assert.deepEqual(_netCumulative(undefined, META), {});
});


// --- edge values ---

test("0 ペア (例: 期首だけマッピングされた科目で動きなし) は 0", () => {
  const r = _netCumulative({ "1010": [0, 0] }, META);
  assert.deepEqual(r, { "1010": 0 });
});

test("負値が出る (誤った仕訳) も計算は通る", () => {
  // 現金 (debit normal) なのに credit > debit → 残高負
  const r = _netCumulative({ "1010": [100, 500] }, META);
  assert.deepEqual(r, { "1010": -400 });
});
