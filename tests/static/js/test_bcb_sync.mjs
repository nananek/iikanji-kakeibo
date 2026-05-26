// bcb_sync (E3-E-3) の単体テスト。
// fetchJournals / saveBcb を DI で差し替えて flow を検証。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/bcb_sync.js",
  import.meta.url,
);
const { syncBalanceCacheForPeriod, syncBalanceCacheForPeriods } = await import(M.href);


function _entry(fp, lines, source = "journal") {
  return { id: fp, fiscal_period: fp, source, lines };
}


test("syncBalanceCacheForPeriod: fetch → compute → save の flow", async () => {
  const fetchedEntries = [
    _entry(3, [
      { account_code: "1010", debit: 1000, credit: 0 },
      { account_code: "5010", debit: 0, credit: 1000 },
    ]),
  ];
  let fetchedFor, savedArgs;
  const res = await syncBalanceCacheForPeriod({
    client: {}, userId: 7, year: 2026, period: 12,
    fetchJournalsImpl: async (args) => {
      fetchedFor = args.fiscalYear;
      return fetchedEntries;
    },
    saveBcbImpl: async (args) => {
      savedArgs = args;
      return { ok: true, updated_at: "2026-05-26T03:00:00+00:00" };
    },
  });
  assert.equal(res.ok, true);
  assert.equal(fetchedFor, 2026);
  assert.equal(savedArgs.year, 2026);
  assert.equal(savedArgs.period, 12);
  assert.deepEqual(savedArgs.balances, {
    "1010": [1000, 0],
    "5010": [0, 1000],
  });
});


test("syncBalanceCacheForPeriod: period 範囲外で throw", async () => {
  await assert.rejects(
    syncBalanceCacheForPeriod({
      client: {}, userId: 7, year: 2026, period: 17,
      fetchJournalsImpl: async () => [],
      saveBcbImpl: async () => ({ ok: true, updated_at: "x" }),
    }),
    /period/,
  );
});


test("syncBalanceCacheForPeriods: 全 period の累計が単調増加", async () => {
  const fetchedEntries = [
    _entry(1, [
      { account_code: "1010", debit: 100, credit: 0 },
      { account_code: "5010", debit: 0, credit: 100 },
    ]),
    _entry(5, [
      { account_code: "1010", debit: 200, credit: 0 },
      { account_code: "5010", debit: 0, credit: 200 },
    ]),
    _entry(10, [
      { account_code: "1010", debit: 300, credit: 0 },
      { account_code: "5010", debit: 0, credit: 300 },
    ]),
  ];
  let fetchCount = 0;
  const savedByPeriod = {};
  const results = await syncBalanceCacheForPeriods({
    client: {}, userId: 7, year: 2026, periods: [1, 5, 12],
    fetchJournalsImpl: async () => {
      fetchCount += 1;
      return fetchedEntries;
    },
    saveBcbImpl: async (args) => {
      savedByPeriod[args.period] = args.balances;
      return { ok: true, updated_at: `t${args.period}` };
    },
  });
  // N+1 回避: fetchJournals は 1 回だけ
  assert.equal(fetchCount, 1);
  assert.equal(results.length, 3);
  // period=1: 100/100
  assert.deepEqual(savedByPeriod[1], { "1010": [100, 0], "5010": [0, 100] });
  // period=5: 累計 300/300
  assert.deepEqual(savedByPeriod[5], { "1010": [300, 0], "5010": [0, 300] });
  // period=12: 累計 600/600
  assert.deepEqual(savedByPeriod[12], { "1010": [600, 0], "5010": [0, 600] });
});


test("syncBalanceCacheForPeriods: 1 つでも period 範囲外で throw", async () => {
  await assert.rejects(
    syncBalanceCacheForPeriods({
      client: {}, userId: 7, year: 2026, periods: [3, 17],
      fetchJournalsImpl: async () => [],
      saveBcbImpl: async () => ({ ok: true, updated_at: "x" }),
    }),
    /period/,
  );
});


test("syncBalanceCacheForPeriods: 空配列で throw", async () => {
  await assert.rejects(
    syncBalanceCacheForPeriods({
      client: {}, userId: 7, year: 2026, periods: [],
      fetchJournalsImpl: async () => [],
      saveBcbImpl: async () => ({ ok: true, updated_at: "x" }),
    }),
    /periods/,
  );
});
