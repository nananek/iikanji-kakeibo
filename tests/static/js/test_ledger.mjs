// ledger.js (Phase E3-C-7) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/ledger.js",
  import.meta.url,
);
const { computeLedger } = await import(M.href);


function entry(id, fp, source, lines, date = "2026-05-01", desc = "") {
  return {
    id, fiscal_period: fp, source, date, description: desc,
    lines: lines.map(([code, debit, credit]) => ({
      account_code: code, debit, credit,
    })),
  };
}


test("空配列で空結果", () => {
  const r = computeLedger([], {
    accountCode: "1010", normalBalance: "debit",
  });
  assert.equal(r.opening_balance, 0);
  assert.deepEqual(r.rows, []);
  assert.equal(r.closing_balance, 0);
  assert.equal(r.total_debit, 0);
  assert.equal(r.total_credit, 0);
});

test("options バリデーション", () => {
  assert.throws(() => computeLedger([], {}), /accountCode/);
  assert.throws(
    () => computeLedger([], { accountCode: "1010" }),
    /normalBalance/,
  );
  assert.throws(() => computeLedger(null, { accountCode: "x", normalBalance: "debit" }), /array/);
});

test("debit normal: balance += debit - credit (運用例: 現金)", () => {
  const entries = [
    entry(1, 1, "journal", [["1010", 1000, 0], ["4010", 0, 1000]]),  // 売上入金
    entry(2, 2, "journal", [["5010", 300, 0], ["1010", 0, 300]]),    // 食費支払
  ];
  const r = computeLedger(entries, {
    accountCode: "1010", normalBalance: "debit",
    openingBalance: 500,  // 期首 500
  });
  assert.equal(r.opening_balance, 500);
  assert.equal(r.rows.length, 2);
  assert.equal(r.rows[0].entry_id, 1);
  assert.equal(r.rows[0].debit, 1000);
  assert.equal(r.rows[0].credit, 0);
  assert.equal(r.rows[0].balance, 1500);  // 500 + 1000
  assert.equal(r.rows[1].debit, 0);
  assert.equal(r.rows[1].credit, 300);
  assert.equal(r.rows[1].balance, 1200);  // 1500 - 300
  assert.equal(r.closing_balance, 1200);
  assert.equal(r.total_debit, 1000);
  assert.equal(r.total_credit, 300);
});

test("credit normal: balance += credit - debit (運用例: 売上)", () => {
  const entries = [
    entry(1, 1, "journal", [["1010", 1000, 0], ["4010", 0, 1000]]),
    entry(2, 1, "journal", [["4010", 200, 0], ["1010", 0, 200]]),  // 返金
  ];
  const r = computeLedger(entries, {
    accountCode: "4010", normalBalance: "credit",
  });
  assert.equal(r.rows[0].balance, 1000);
  assert.equal(r.rows[1].balance, 800);  // 1000 - 200
});

test("counterparts: 同 entry 内の他科目を集約", () => {
  const entries = [
    entry(1, 5, "journal", [
      ["1010", 1000, 0],
      ["5010", 300, 0],
      ["5020", 200, 0],
      ["4010", 0, 1500],
    ]),
  ];
  const r = computeLedger(entries, {
    accountCode: "1010", normalBalance: "debit",
  });
  // 1010 以外の科目: 4010, 5010, 5020 がソート済で counterparts に
  assert.equal(r.rows[0].counterparts, "4010, 5010, 5020");
});

test("対象科目を含まない entry はスキップ", () => {
  const entries = [
    entry(1, 1, "journal", [["1020", 500, 0], ["4010", 0, 500]]),
    entry(2, 1, "journal", [["1010", 200, 0], ["4010", 0, 200]]),
  ];
  const r = computeLedger(entries, {
    accountCode: "1010", normalBalance: "debit",
  });
  assert.equal(r.rows.length, 1);
  assert.equal(r.rows[0].entry_id, 2);
});

test("fiscal_period 範囲フィルタ", () => {
  const entries = [
    entry(1, 0, "journal", [["1010", 100, 0]]),    // 期首
    entry(2, 3, "journal", [["1010", 200, 0]]),    // 3 月
    entry(3, 5, "journal", [["1010", 300, 0]]),    // 5 月
    entry(4, 13, "journal", [["1010", 400, 0]]),   // 決算整理
  ];
  const r = computeLedger(entries, {
    accountCode: "1010", normalBalance: "debit",
    fiscalPeriodFrom: 4, fiscalPeriodTo: 5,
  });
  assert.equal(r.rows.length, 1);
  assert.equal(r.rows[0].entry_id, 3);
});

test("includeClosing=false で source='closing' を除外", () => {
  const entries = [
    entry(1, 16, "journal", [["3020", 0, 1000]]),
    entry(2, 16, "closing", [["3020", 500, 0]]),
  ];
  const r = computeLedger(entries, {
    accountCode: "3020", normalBalance: "credit",
    fiscalPeriodTo: 16, includeClosing: false,
  });
  assert.equal(r.rows.length, 1);
  assert.equal(r.rows[0].credit, 1000);
});

test("includeClosing=true (default) で closing も含む", () => {
  const entries = [
    entry(1, 16, "journal", [["3020", 0, 1000]]),
    entry(2, 16, "closing", [["3020", 500, 0]]),
  ];
  const r = computeLedger(entries, {
    accountCode: "3020", normalBalance: "credit",
    fiscalPeriodTo: 16,
  });
  assert.equal(r.rows.length, 2);
});

test("entry.id 昇順でソート (id 順は概ね時系列)", () => {
  const entries = [
    entry(30, 1, "journal", [["1010", 300, 0]]),
    entry(10, 1, "journal", [["1010", 100, 0]]),
    entry(20, 1, "journal", [["1010", 200, 0]]),
  ];
  const r = computeLedger(entries, {
    accountCode: "1010", normalBalance: "debit",
  });
  assert.deepEqual(r.rows.map(x => x.entry_id), [10, 20, 30]);
});

test("openingBalance default = 0", () => {
  const entries = [entry(1, 1, "journal", [["1010", 100, 0]])];
  const r = computeLedger(entries, {
    accountCode: "1010", normalBalance: "debit",
  });
  assert.equal(r.opening_balance, 0);
  assert.equal(r.rows[0].balance, 100);
});

test("同 entry 内に対象科目の複数 line があれば合算 (debit+debit)", () => {
  const entries = [
    entry(1, 1, "journal", [
      ["1010", 300, 0],
      ["1010", 200, 0],   // 同科目 (実用上少ないが防御的)
      ["4010", 0, 500],
    ]),
  ];
  const r = computeLedger(entries, {
    accountCode: "1010", normalBalance: "debit",
  });
  assert.equal(r.rows[0].debit, 500);
  assert.equal(r.rows[0].balance, 500);
});

test("account_code が null の counterpart line は無視", () => {
  const entries = [
    {id: 1, fiscal_period: 5, source: "journal", date: "2026-05-01",
     description: "",
     lines: [
       {account_code: "1010", debit: 100, credit: 0},
       {account_code: null, debit: 0, credit: 100},  // 復号失敗
     ]},
  ];
  const r = computeLedger(entries, {
    accountCode: "1010", normalBalance: "debit",
  });
  assert.equal(r.rows.length, 1);
  assert.equal(r.rows[0].counterparts, "");  // null は除外
});
