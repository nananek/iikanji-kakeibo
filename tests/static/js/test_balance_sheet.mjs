// balance_sheet.js (Phase E3-C-4) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/balance_sheet.js",
  import.meta.url,
);
const { computeBalanceSheet } = await import(M.href);


// --- fixtures ---

const TYPES = {
  "1010": "asset",     "1020": "asset",
  "2010": "liability", "2020": "liability",
  "3010": "equity",    "3020": "equity",
  "4010": "revenue",   "5010": "expense",
};
const NORMAL = {
  "1010": "debit",  "1020": "debit",
  "2010": "credit", "2020": "credit",
  "3010": "credit", "3020": "credit",
  "4010": "credit", "5010": "debit",
};
const NAMES = {
  "1010": "現金", "1020": "預金",
  "2010": "買掛金", "2020": "借入金",
  "3010": "元入金", "3020": "繰越利益",
  "4010": "売上", "5010": "食費",
};

function entry(id, fp, source, lines) {
  return {
    id, fiscal_period: fp, source,
    lines: lines.map(([code, debit, credit]) => ({
      account_code: code, debit, credit,
    })),
  };
}


// --- tests ---

test("空配列で全 0", () => {
  const r = computeBalanceSheet([], {
    accountTypeByCode: TYPES, normalBalanceByCode: NORMAL,
  });
  assert.equal(r.total_assets, 0);
  assert.equal(r.total_liabilities, 0);
  assert.equal(r.total_equity, 0);
  assert.equal(r.net_income, 0);
  assert.equal(r.has_closing, false);
  assert.deepEqual(r.assets, []);
});

test("options 不足で TypeError", () => {
  assert.throws(() => computeBalanceSheet([], {}), /accountTypeByCode/);
  assert.throws(
    () => computeBalanceSheet([], { accountTypeByCode: {} }),
    /normalBalanceByCode/,
  );
});

test("資産科目: debit normal は debit-credit", () => {
  // 現金入金 1000 → 売上 1000
  const entries = [
    entry(1, 5, "journal", [["1010", 1000, 0], ["4010", 0, 1000]]),
  ];
  const r = computeBalanceSheet(entries, {
    accountTypeByCode: TYPES, normalBalanceByCode: NORMAL,
    accountNameByCode: NAMES,
  });
  assert.equal(r.assets.length, 1);
  assert.equal(r.assets[0].account_code, "1010");
  assert.equal(r.assets[0].account_name, "現金");
  assert.equal(r.assets[0].balance, 1000);
  assert.equal(r.total_assets, 1000);
  // 損益振替前なので net_income (= P/L 純利益 1000) が equity に加算される
  assert.equal(r.net_income, 1000);
  assert.equal(r.has_closing, false);
  assert.equal(r.total_liability_and_equity, 1000);  // 0 + 0 + 1000
});

test("負債科目: credit normal は credit-debit", () => {
  const entries = [
    entry(1, 5, "journal", [["1010", 5000, 0], ["2020", 0, 5000]]),  // 借入
  ];
  const r = computeBalanceSheet(entries, {
    accountTypeByCode: TYPES, normalBalanceByCode: NORMAL,
  });
  assert.equal(r.liabilities[0].balance, 5000);
  assert.equal(r.total_liabilities, 5000);
});

test("純資産科目: credit normal", () => {
  const entries = [
    entry(1, 0, "journal", [["1010", 100000, 0], ["3010", 0, 100000]]),
  ];
  const r = computeBalanceSheet(entries, {
    accountTypeByCode: TYPES, normalBalanceByCode: NORMAL,
  });
  assert.equal(r.equities[0].balance, 100000);
  assert.equal(r.total_equity, 100000);
});

test("balance==0 の科目は breakdown から除外", () => {
  const entries = [
    entry(1, 5, "journal", [["1010", 100, 0]]),
    entry(2, 5, "journal", [["1010", 0, 100]]),
  ];
  const r = computeBalanceSheet(entries, {
    accountTypeByCode: TYPES, normalBalanceByCode: NORMAL,
  });
  assert.equal(r.assets.length, 0);
  assert.equal(r.total_assets, 0);
});

test("P/L 科目 (revenue/expense) は BS には含まれない", () => {
  const entries = [
    entry(1, 5, "journal", [["1010", 1000, 0], ["4010", 0, 1000]]),
  ];
  const r = computeBalanceSheet(entries, {
    accountTypeByCode: TYPES, normalBalanceByCode: NORMAL,
  });
  // assets に 1010 はあるが equities/liabilities に 4010 はない
  assert.equal(r.equities.length, 0);
  assert.equal(r.liabilities.length, 0);
  // net_income に反映
  assert.equal(r.net_income, 1000);
});

test("has_closing=true なら net_income はゼロ (繰越利益に含まれる前提)", () => {
  // 通常仕訳 + closing 仕訳
  const entries = [
    entry(1, 5, "journal", [["1010", 1000, 0], ["4010", 0, 1000]]),
    // 損益振替: 売上を繰越利益へ
    entry(2, 16, "closing", [["4010", 1000, 0], ["3020", 0, 1000]]),
  ];
  const r = computeBalanceSheet(entries, {
    accountTypeByCode: TYPES, normalBalanceByCode: NORMAL,
  });
  assert.equal(r.has_closing, true);
  assert.equal(r.net_income, 0);  // 振替済みなので加算しない
  // 繰越利益 (3020) に 1000 が反映
  assert.equal(r.equities[0].account_code, "3020");
  assert.equal(r.equities[0].balance, 1000);
  // 現金 1000 = 繰越利益 1000 (BS バランス)
  assert.equal(r.total_assets, 1000);
  assert.equal(r.total_liability_and_equity, 1000);
});

test("複数科目の breakdown とソート", () => {
  const entries = [
    entry(1, 0, "journal", [
      ["1010", 1000, 0],   // 現金
      ["1020", 5000, 0],   // 預金
      ["3010", 0, 6000],   // 元入金
    ]),
    entry(2, 5, "journal", [
      ["1020", 2000, 0],   // 預金入金 (売上)
      ["4010", 0, 2000],
    ]),
    entry(3, 5, "journal", [
      ["5010", 500, 0],    // 食費
      ["1010", 0, 500],
    ]),
  ];
  const r = computeBalanceSheet(entries, {
    accountTypeByCode: TYPES, normalBalanceByCode: NORMAL,
    accountNameByCode: NAMES,
  });
  assert.equal(r.assets.length, 2);
  assert.equal(r.assets[0].account_code, "1010");  // 500
  assert.equal(r.assets[0].balance, 500);
  assert.equal(r.assets[1].account_code, "1020");  // 7000
  assert.equal(r.assets[1].balance, 7000);
  assert.equal(r.total_assets, 7500);
  assert.equal(r.equities[0].account_code, "3010");
  assert.equal(r.equities[0].balance, 6000);
  // net_income = 売上 2000 - 食費 500 = 1500 (損益振替前)
  assert.equal(r.net_income, 1500);
  // 6000 (元入金) + 0 + 1500 (純利益) = 7500
  assert.equal(r.total_liability_and_equity, 7500);
});

test("account_code 不明・null は無視", () => {
  const entries = [
    {id: 1, fiscal_period: 5, source: "journal", lines: [
      {account_code: null, debit: 100, credit: 0},   // 復号失敗
      {account_code: "UNKNOWN", debit: 200, credit: 0},  // マスタにない
      {account_code: "1010", debit: 300, credit: 0},
    ]},
  ];
  const r = computeBalanceSheet(entries, {
    accountTypeByCode: TYPES, normalBalanceByCode: NORMAL,
  });
  assert.equal(r.assets.length, 1);
  assert.equal(r.assets[0].balance, 300);
});
