// Tests for client-side accounting helpers (buildCashbookEntry / buildTransferEntry).

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/entries_builder.js",
  import.meta.url,
);
const { buildCashbookEntry, buildTransferEntry } = await import(M.href);


// --- buildCashbookEntry ---


test("expense: 費目=借方 / 支払口座=貸方", () => {
  const e = buildCashbookEntry({
    date: "2026-02-15",
    description: "ランチ",
    transactionType: "expense",
    paymentAccountCode: "1010",
    categoryAccountCode: "5010",
    amount: 800,
  });
  assert.equal(e.date, "2026-02-15");
  assert.equal(e.description, "ランチ");
  assert.equal(e.source, "cashbook");
  assert.equal(e.fiscal_period, null);
  assert.deepEqual(e.lines, [
    { account_code: "5010", debit: 800, credit: 0 },
    { account_code: "1010", debit: 0, credit: 800 },
  ]);
});

test("income: 入金口座=借方 / 収益科目=貸方", () => {
  const e = buildCashbookEntry({
    date: "2026-02-25",
    description: "給与",
    transactionType: "income",
    paymentAccountCode: "1020",
    categoryAccountCode: "4010",
    amount: 300000,
  });
  assert.deepEqual(e.lines, [
    { account_code: "1020", debit: 300000, credit: 0 },
    { account_code: "4010", debit: 0, credit: 300000 },
  ]);
});

test("負金額 (expense): 借方・貸方が入れ替わる (返金処理)", () => {
  const e = buildCashbookEntry({
    date: "2026-02-15",
    description: "返金",
    transactionType: "expense",
    paymentAccountCode: "1010",
    categoryAccountCode: "5010",
    amount: -500,
  });
  // 通常 expense は 5010=debit / 1010=credit、負だと逆 = 1010=debit / 5010=credit
  assert.deepEqual(e.lines, [
    { account_code: "1010", debit: 500, credit: 0 },
    { account_code: "5010", debit: 0, credit: 500 },
  ]);
});

test("source / fiscalPeriod を上書きできる (csv 取込等)", () => {
  const e = buildCashbookEntry({
    date: "2026-02-15",
    description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010",
    categoryAccountCode: "5010",
    amount: 100,
    source: "csv",
    fiscalPeriod: 2,
  });
  assert.equal(e.source, "csv");
  assert.equal(e.fiscal_period, 2);
});

test("不正な transactionType で TypeError", () => {
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "invalid",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
  }), TypeError);
});

test("amount=0 で TypeError", () => {
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 0,
  }), TypeError);
});

test("float amount で TypeError (黙って丸めない)", () => {
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100.5,
  }), TypeError);
});

test("payment / category account_code 欠落で TypeError", () => {
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "", categoryAccountCode: "5010", amount: 100,
  }), TypeError);
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "", amount: 100,
  }), TypeError);
});


// --- buildTransferEntry ---


test("transfer: to=借方 / from=貸方", () => {
  const e = buildTransferEntry({
    date: "2026-02-20",
    description: "現金→普通預金",
    fromAccountCode: "1010",
    toAccountCode: "1020",
    amount: 50000,
  });
  assert.deepEqual(e.lines, [
    { account_code: "1020", debit: 50000, credit: 0 },
    { account_code: "1010", debit: 0, credit: 50000 },
  ]);
});

test("transfer: 負金額で from/to が反転 (打消し)", () => {
  const e = buildTransferEntry({
    date: "2026-02-20",
    description: "取消",
    fromAccountCode: "1010",
    toAccountCode: "1020",
    amount: -50000,
  });
  assert.deepEqual(e.lines, [
    { account_code: "1010", debit: 50000, credit: 0 },
    { account_code: "1020", debit: 0, credit: 50000 },
  ]);
});

test("transfer: 同一 from/to で TypeError", () => {
  assert.throws(() => buildTransferEntry({
    date: "2026-02-20", description: "x",
    fromAccountCode: "1010", toAccountCode: "1010", amount: 100,
  }), TypeError);
});

test("transfer: amount=0 で TypeError", () => {
  assert.throws(() => buildTransferEntry({
    date: "2026-02-20", description: "x",
    fromAccountCode: "1010", toAccountCode: "1020", amount: 0,
  }), TypeError);
});

test("date 欠落で TypeError (cashbook / transfer 両方)", () => {
  assert.throws(() => buildCashbookEntry({
    description: "x", transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
  }), TypeError);
  assert.throws(() => buildTransferEntry({
    description: "x", fromAccountCode: "1010", toAccountCode: "1020", amount: 100,
  }), TypeError);
});

test("description 省略時は空文字", () => {
  const e = buildCashbookEntry({
    date: "2026-02-15",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
  });
  assert.equal(e.description, "");
});
