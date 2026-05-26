// Tests for planBcbSync (Phase E3-E-4 hook の純粋関数部分)。
// _run() (DOM + dynamic imports) はブラウザ専用なのでテスト対象外。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/bcb_sync_hook.mjs",
  import.meta.url,
);
const { planBcbSync } = await import(M.href);


test("closed_period=-1 (未確定): sync 不要、stale もなし", () => {
  const r = planBcbSync(new Set(), -1);
  assert.deepEqual(r, { toSync: [], staleFromPeriod: null });
});


test("closed_period=3、existing 空: 0..3 を sync", () => {
  const r = planBcbSync(new Set(), 3);
  assert.deepEqual(r.toSync, [0, 1, 2, 3]);
  assert.equal(r.staleFromPeriod, null);
});


test("closed_period=3、一部 existing: 欠けてる period だけ sync", () => {
  const r = planBcbSync(new Set([0, 2]), 3);
  assert.deepEqual(r.toSync, [1, 3]);
  assert.equal(r.staleFromPeriod, null);
});


test("closed_period=3、全て existing: sync 不要", () => {
  const r = planBcbSync(new Set([0, 1, 2, 3]), 3);
  assert.deepEqual(r.toSync, []);
  assert.equal(r.staleFromPeriod, null);
});


test("closed_period=15: 0..15 + 16 (損益振替済) を sync 対象に追加", () => {
  const r = planBcbSync(new Set(), 15);
  assert.deepEqual(
    r.toSync,
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
  );
  assert.equal(r.staleFromPeriod, null);
});


test("closed_period=15、16 が既に existing: 16 は sync 対象に入れない", () => {
  const r = planBcbSync(new Set([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]), 15);
  assert.deepEqual(r.toSync, []);
});


test("closed_period=3、existing に 5,7,10 (stale) がある: stale 削除起点=4", () => {
  // reopen で 3 まで戻されたが、以前 7 まで確定していて BCB が残っているケース
  const r = planBcbSync(new Set([0, 1, 2, 3, 5, 7, 10]), 3);
  assert.deepEqual(r.toSync, []);  // 0..3 は全て existing
  assert.equal(r.staleFromPeriod, 4);  // 4 以降を DELETE → 5, 7, 10 が消える
});


test("closed_period=-1 (未確定) で stale BCB がある場合: staleFromPeriod=0", () => {
  // 完全 reopen で全 period 戻されたが BCB が残っているケース
  const r = planBcbSync(new Set([0, 5]), -1);
  assert.deepEqual(r.toSync, []);
  assert.equal(r.staleFromPeriod, 0);  // 0 から全削除
});


test("closed_period=15: stale 削除しない (period 16 は保持)", () => {
  // 15 まで確定 → 16 (損益振替済) BCB は保持されるべき
  const r = planBcbSync(new Set([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]), 15);
  assert.deepEqual(r.toSync, []);
  assert.equal(r.staleFromPeriod, null);  // 16 を消さない
});


test("closed_period=3、existing は 0..3 のみ: stale なし", () => {
  const r = planBcbSync(new Set([0, 1, 2, 3]), 3);
  assert.equal(r.staleFromPeriod, null);
});
