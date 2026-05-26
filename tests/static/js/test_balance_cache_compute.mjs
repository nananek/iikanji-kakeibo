// computeBalanceCache (E3-E-3) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/balance_cache.js",
  import.meta.url,
);
const { computeBalanceCache } = await import(M.href);


function entry({ id = 1, fp = null, source = "journal", date = null, lines = [] } = {}) {
  return { id, fiscal_period: fp, source, date, lines };
}


test("空配列 → 空 dict", () => {
  assert.deepEqual(computeBalanceCache([], { period: 12 }), {});
});


test("period 内の entry を集計", () => {
  const entries = [
    entry({
      fp: 3,
      lines: [
        { account_code: "1010", debit: 1000, credit: 0 },
        { account_code: "5010", debit: 0, credit: 1000 },
      ],
    }),
    entry({
      fp: 5,
      lines: [
        { account_code: "1010", debit: 500, credit: 0 },
        { account_code: "5010", debit: 0, credit: 500 },
      ],
    }),
  ];
  assert.deepEqual(computeBalanceCache(entries, { period: 12 }), {
    "1010": [1500, 0],
    "5010": [0, 1500],
  });
});


test("period 超過の entry はスキップ", () => {
  const entries = [
    entry({ fp: 3, lines: [{ account_code: "1010", debit: 100, credit: 0 }] }),
    entry({ fp: 6, lines: [{ account_code: "1010", debit: 999, credit: 0 }] }),
  ];
  // period=5 だと fp=6 は除外、fp=3 だけ集計
  assert.deepEqual(computeBalanceCache(entries, { period: 5 }), {
    "1010": [100, 0],
  });
});


test("source=closing は period<16 で除外、period=16 で含まれる", () => {
  const entries = [
    entry({ fp: 16, source: "closing", lines: [
      { account_code: "3020", debit: 0, credit: 500 },
    ] }),
  ];
  // period=15: 除外 (空)
  assert.deepEqual(computeBalanceCache(entries, { period: 15 }), {});
  // period=16: 含まれる
  assert.deepEqual(computeBalanceCache(entries, { period: 16 }), {
    "3020": [0, 500],
  });
});


test("includeClosing=true で明示すれば period<16 でも含む", () => {
  const entries = [
    entry({ fp: 12, source: "closing", lines: [
      { account_code: "3020", debit: 0, credit: 100 },
    ] }),
  ];
  assert.deepEqual(
    computeBalanceCache(entries, { period: 12, includeClosing: true }),
    { "3020": [0, 100] },
  );
});


test("(0, 0) の account は結果から除外", () => {
  const entries = [
    entry({ fp: 3, lines: [
      { account_code: "1010", debit: 100, credit: 100 },  // 相殺で 0,0 ではないが
      { account_code: "5010", debit: 0, credit: 0 },      // 完全 0 行
    ] }),
  ];
  // 1010 は debit=100, credit=100 → 結果に残る (両方 0 ではない)
  // 5010 は完全 0 → 除外
  assert.deepEqual(computeBalanceCache(entries, { period: 12 }), {
    "1010": [100, 100],
  });
});


test("fiscal_period が null の旧データは date.month で fallback", () => {
  const entries = [
    entry({ fp: null, date: "2026-04-15", lines: [
      { account_code: "1010", debit: 100, credit: 0 },
    ] }),
  ];
  // period=3 だと 4 月 (fp=4) は範囲外
  assert.deepEqual(computeBalanceCache(entries, { period: 3 }), {});
  // period=4 だと範囲内
  assert.deepEqual(computeBalanceCache(entries, { period: 4 }), {
    "1010": [100, 0],
  });
});


test("account_code=null の line はスキップ (復号失敗等)", () => {
  const entries = [
    entry({ fp: 3, lines: [
      { account_code: "1010", debit: 100, credit: 0 },
      { account_code: null, debit: 50, credit: 0 },  // 復号失敗
    ] }),
  ];
  assert.deepEqual(computeBalanceCache(entries, { period: 12 }), {
    "1010": [100, 0],
  });
});


test("非配列 entries で TypeError", () => {
  assert.throws(
    () => computeBalanceCache(null, { period: 12 }),
    TypeError,
  );
});


test("period 未指定 / 範囲外で TypeError", () => {
  assert.throws(
    () => computeBalanceCache([], { period: -1 }),
    TypeError,
  );
  assert.throws(
    () => computeBalanceCache([], { period: 17 }),
    TypeError,
  );
  assert.throws(
    () => computeBalanceCache([], {}),
    TypeError,
  );
});


test("複数 period の累計関係: period が大きいほど cumulative が単調増加", () => {
  const entries = [
    entry({ fp: 1, lines: [{ account_code: "1010", debit: 100, credit: 0 }] }),
    entry({ fp: 5, lines: [{ account_code: "1010", debit: 200, credit: 0 }] }),
    entry({ fp: 10, lines: [{ account_code: "1010", debit: 300, credit: 0 }] }),
  ];
  assert.deepEqual(computeBalanceCache(entries, { period: 1 }), { "1010": [100, 0] });
  assert.deepEqual(computeBalanceCache(entries, { period: 5 }), { "1010": [300, 0] });
  assert.deepEqual(computeBalanceCache(entries, { period: 12 }), { "1010": [600, 0] });
});
