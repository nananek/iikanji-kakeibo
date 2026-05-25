// Tests for the pure compareTrialBalance helper.
// _run() (DOM + dynamic imports) is browser-only and not covered here.

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/trial_balance_validator.mjs",
  import.meta.url,
);
const { compareTrialBalance } = await import(M.href);


test("compareTrialBalance: 完全一致で diffs 空", () => {
  const server = [
    { code: "1010", debit: 1000, credit: 0 },
    { code: "5010", debit: 500, credit: 0 },
  ];
  const js = [
    { account_code: "1010", debit: 1000, credit: 0 },
    { account_code: "5010", debit: 500, credit: 0 },
  ];
  assert.deepEqual(compareTrialBalance(server, js), []);
});

test("compareTrialBalance: 値の不一致を mismatch として検出", () => {
  const server = [{ code: "1010", debit: 1000, credit: 0 }];
  const js = [{ account_code: "1010", debit: 999, credit: 0 }];
  const d = compareTrialBalance(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "mismatch");
  assert.equal(d[0].code, "1010");
  assert.deepEqual(d[0].server, { debit: 1000, credit: 0 });
  assert.deepEqual(d[0].client, { debit: 999, credit: 0 });
});

test("compareTrialBalance: サーバにあるが JS にない → missing_in_client", () => {
  const server = [{ code: "1010", debit: 100, credit: 0 }];
  const js = [];
  const d = compareTrialBalance(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "missing_in_client");
  assert.equal(d[0].code, "1010");
});

test("compareTrialBalance: JS にあるがサーバにない → extra_in_client (非ゼロのみ)", () => {
  const server = [{ code: "1010", debit: 100, credit: 0 }];
  const js = [
    { account_code: "1010", debit: 100, credit: 0 },
    { account_code: "9999", debit: 50, credit: 0 },  // 余分
    { account_code: "9998", debit: 0, credit: 0 },   // ゼロは無視
  ];
  const d = compareTrialBalance(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "extra_in_client");
  assert.equal(d[0].code, "9999");
});

test("compareTrialBalance: credit のみ不一致でも検出", () => {
  const server = [{ code: "4010", debit: 0, credit: 1000 }];
  const js = [{ account_code: "4010", debit: 0, credit: 999 }];
  const d = compareTrialBalance(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "mismatch");
});

test("compareTrialBalance: 空配列同士は diffs 空", () => {
  assert.deepEqual(compareTrialBalance([], []), []);
});

test("compareTrialBalance: サーバ側がゼロ行 → missing_in_client に出さない", () => {
  // B/S 勘定で当期取引なし (period 集計はゼロだが opening balance あり) のケース。
  // client-side computeTrialBalance はこの科目をスキップするので、
  // missing_in_client にすると常に偽陽性になる。
  const server = [{ code: "1010", debit: 0, credit: 0 }];
  const js = [];
  assert.deepEqual(compareTrialBalance(server, js), []);
});
