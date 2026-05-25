// Tests for the pure compareLedger helper.
// _run() (DOM + dynamic imports) is browser-only and not covered here.

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/ledger_validator.mjs",
  import.meta.url,
);
const { compareLedger } = await import(M.href);


test("完全一致で diffs 空", () => {
  const server = [
    { entry_id: 1, debit: 100, credit: 0, balance: 100 },
    { entry_id: 2, debit: 0, credit: 30, balance: 70 },
  ];
  const js = {
    rows: [
      { entry_id: 1, debit: 100, credit: 0, balance: 100 },
      { entry_id: 2, debit: 0, credit: 30, balance: 70 },
    ],
  };
  assert.deepEqual(compareLedger(server, js), []);
});

test("balance 不一致は mismatch (累積残高のズレを検知)", () => {
  const server = [{ entry_id: 1, debit: 100, credit: 0, balance: 100 }];
  const js = {
    rows: [{ entry_id: 1, debit: 100, credit: 0, balance: 99 }],
  };
  const d = compareLedger(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "mismatch");
  assert.equal(d[0].entry_id, 1);
  assert.equal(d[0].server.balance, 100);
  assert.equal(d[0].client.balance, 99);
});

test("debit 不一致も mismatch", () => {
  const server = [{ entry_id: 1, debit: 100, credit: 0, balance: 100 }];
  const js = {
    rows: [{ entry_id: 1, debit: 99, credit: 0, balance: 100 }],
  };
  const d = compareLedger(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "mismatch");
});

test("credit 不一致も mismatch", () => {
  const server = [{ entry_id: 1, debit: 0, credit: 50, balance: -50 }];
  const js = {
    rows: [{ entry_id: 1, debit: 0, credit: 49, balance: -50 }],
  };
  const d = compareLedger(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "mismatch");
});

test("サーバにあるが JS にない → missing_in_client", () => {
  const server = [{ entry_id: 1, debit: 100, credit: 0, balance: 100 }];
  const js = { rows: [] };
  const d = compareLedger(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "missing_in_client");
  assert.equal(d[0].entry_id, 1);
});

test("JS にあるがサーバにない → extra_in_client", () => {
  // ledger は entry_id 単位 (科目を含む行のみ) で並ぶため、ゼロ行は
  // そもそも computeLedger が返さない。よって extra のゼロ抑制は不要。
  const server = [{ entry_id: 1, debit: 100, credit: 0, balance: 100 }];
  const js = {
    rows: [
      { entry_id: 1, debit: 100, credit: 0, balance: 100 },
      { entry_id: 2, debit: 50, credit: 0, balance: 150 },
    ],
  };
  const d = compareLedger(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "extra_in_client");
  assert.equal(d[0].entry_id, 2);
});

test("空配列同士は diffs 空", () => {
  assert.deepEqual(compareLedger([], { rows: [] }), []);
});
