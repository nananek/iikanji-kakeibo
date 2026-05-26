// Phase E3-F-3e: composeLedgerView の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/ledger_view.js",
  import.meta.url,
);
const { composeLedgerView } = await import(M.href);


// --- helpers ---

function row(opts) {
  return {
    entry_id: opts.entry_id, fiscal_period: opts.fiscal_period ?? 5,
    date: opts.date ?? "2026-05-15",
    description: opts.description ?? "x",
    debit: opts.debit ?? 0, credit: opts.credit ?? 0,
    balance: opts.balance ?? 0, counterparts: opts.counterparts ?? "",
  };
}

function ledgerResult(rows = [], extras = {}) {
  return {
    opening_balance: extras.opening_balance ?? 0,
    rows,
    closing_balance: extras.closing_balance ?? 0,
    total_debit: extras.total_debit ?? 0,
    total_credit: extras.total_credit ?? 0,
  };
}

const ACCOUNTS_META = {
  "1010": { name: "現金" },
  "5010": { name: "食費" },
  "4010": { name: "給与" },
};


// --- arg validation ---

test("ledgerResult が object でないと TypeError", () => {
  assert.throws(() => composeLedgerView(null), /object/);
});

test("ledgerResult.rows が配列でないと TypeError", () => {
  assert.throws(() => composeLedgerView({ rows: null }), /array/);
});


// --- empty ---

test("空 rows でも opening/closing/total を保持", () => {
  const v = composeLedgerView(ledgerResult([], {
    opening_balance: 1000, closing_balance: 1000,
  }), { accountsMeta: ACCOUNTS_META });
  assert.equal(v.opening_balance, 1000);
  assert.equal(v.closing_balance, 1000);
  assert.equal(v.rows.length, 0);
});


// --- counter codes -> names ---

test("counterparts は accountsMeta で name に変換される", () => {
  const v = composeLedgerView(ledgerResult([
    row({ entry_id: 1, counterparts: "1010, 4010", debit: 0, credit: 1000, balance: 1000 }),
  ]), { accountsMeta: ACCOUNTS_META });
  assert.equal(v.rows[0].counter_account_names, "現金, 給与");
});

test("accountsMeta にない code はそのまま残る", () => {
  const v = composeLedgerView(ledgerResult([
    row({ entry_id: 1, counterparts: "9999", debit: 100, credit: 0, balance: 100 }),
  ]), { accountsMeta: ACCOUNTS_META });
  assert.equal(v.rows[0].counter_account_names, "9999");
});


// --- entries meta merge ---

test("entriesMeta から is_readonly / voucher_id / entry_number をマージ", () => {
  const v = composeLedgerView(ledgerResult([
    row({ entry_id: 42, debit: 100, balance: 100 }),
  ]), {
    accountsMeta: ACCOUNTS_META,
    entriesMeta: {
      42: { is_readonly: true, voucher_id: 7, entry_number: 123 },
    },
  });
  assert.equal(v.rows[0].is_readonly, true);
  assert.equal(v.rows[0].voucher_id, 7);
  assert.equal(v.rows[0].entry_number, 123);
});

test("entriesMeta なしのデフォルト値は readonly=false / voucher_id=null / entry_number=null", () => {
  const v = composeLedgerView(ledgerResult([
    row({ entry_id: 1, debit: 1 }),
  ]), { accountsMeta: ACCOUNTS_META });
  assert.equal(v.rows[0].is_readonly, false);
  assert.equal(v.rows[0].voucher_id, null);
  assert.equal(v.rows[0].entry_number, null);
});


// --- sort order ---

test("sortOrder=asc は元順序を維持", () => {
  const v = composeLedgerView(ledgerResult([
    row({ entry_id: 1 }), row({ entry_id: 2 }), row({ entry_id: 3 }),
  ]), { accountsMeta: ACCOUNTS_META, sortOrder: "asc" });
  assert.deepEqual(v.rows.map((r) => r.entry_id), [1, 2, 3]);
  assert.equal(v.sort_order, "asc");
});

test("sortOrder=desc は逆順", () => {
  const v = composeLedgerView(ledgerResult([
    row({ entry_id: 1 }), row({ entry_id: 2 }), row({ entry_id: 3 }),
  ]), { accountsMeta: ACCOUNTS_META, sortOrder: "desc" });
  assert.deepEqual(v.rows.map((r) => r.entry_id), [3, 2, 1]);
  assert.equal(v.sort_order, "desc");
});

test("sortOrder の不正値は asc にフォールバック", () => {
  const v = composeLedgerView(ledgerResult([
    row({ entry_id: 1 }), row({ entry_id: 2 }),
  ]), { accountsMeta: ACCOUNTS_META, sortOrder: "invalid" });
  assert.equal(v.sort_order, "asc");
  assert.deepEqual(v.rows.map((r) => r.entry_id), [1, 2]);
});
