// profit_loss.js (Phase E3-C-3) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/profit_loss.js",
  import.meta.url,
);
const { computeProfitLoss } = await import(M.href);


// --- fixtures ---

const ACCOUNTS = {
  "1010": "asset",     // 現金
  "2010": "liability", // 買掛金
  "3010": "equity",    // 元入金
  "4010": "revenue",   // 売上
  "4020": "revenue",   // 雑収入
  "5010": "expense",   // 食費
  "5020": "expense",   // 消耗品費
};

const NAMES = {
  "4010": "売上高", "4020": "雑収入", "5010": "食費", "5020": "消耗品費",
};

// E3-F PR-D-6-3b: レポート集計は保持列 fiscal_month / is_closing を読む
// (平文 fiscal_period / source は API から撤去済)。
function entry(id, fp, source, lines) {
  return {
    id, fiscal_month: fp, is_closing: source === "closing",
    lines: lines.map(([code, debit, credit]) => ({
      account_code: code, debit, credit,
    })),
  };
}


// --- tests ---

test("空配列で全 0", () => {
  const r = computeProfitLoss([], { accountTypeByCode: ACCOUNTS });
  assert.deepEqual(r, {
    income_total: 0,
    expense_total: 0,
    net_income: 0,
    income_breakdown: [],
    expense_breakdown: [],
  });
});

test("accountTypeByCode 未指定で TypeError", () => {
  assert.throws(() => computeProfitLoss([], {}), /accountTypeByCode/);
});

test("entries が配列でないと TypeError", () => {
  assert.throws(() => computeProfitLoss(null, { accountTypeByCode: {} }), /array/);
});

test("基本: 売上 (credit) と費用 (debit) の集計", () => {
  // 売上 10000 / 現金 10000、食費 3000 / 現金 3000
  const entries = [
    entry(1, 5, "journal", [["1010", 10000, 0], ["4010", 0, 10000]]),
    entry(2, 5, "journal", [["5010", 3000, 0], ["1010", 0, 3000]]),
  ];
  const r = computeProfitLoss(entries, {
    accountTypeByCode: ACCOUNTS, accountNameByCode: NAMES,
  });
  assert.equal(r.income_total, 10000);
  assert.equal(r.expense_total, 3000);
  assert.equal(r.net_income, 7000);
  assert.equal(r.income_breakdown.length, 1);
  assert.equal(r.income_breakdown[0].account_code, "4010");
  assert.equal(r.income_breakdown[0].account_name, "売上高");
  assert.equal(r.income_breakdown[0].amount, 10000);
  assert.equal(r.expense_breakdown[0].amount, 3000);
});

test("収益の振替/返金 (debit) は income 側で減算", () => {
  // 売上戻り: 売上 1000 / 現金 1000 (借方売上 = 収益のマイナス)
  const entries = [
    entry(1, 5, "journal", [["1010", 5000, 0], ["4010", 0, 5000]]),
    entry(2, 5, "journal", [["4010", 1000, 0], ["1010", 0, 1000]]),  // 返金
  ];
  const r = computeProfitLoss(entries, { accountTypeByCode: ACCOUNTS });
  assert.equal(r.income_total, 4000);  // 5000 - 1000
  assert.equal(r.income_breakdown[0].amount, 4000);
});

test("amount==0 の科目は breakdown から除外", () => {
  // 借方/貸方完全相殺
  const entries = [
    entry(1, 5, "journal", [["4010", 100, 0]]),
    entry(2, 5, "journal", [["4010", 0, 100]]),
  ];
  const r = computeProfitLoss(entries, { accountTypeByCode: ACCOUNTS });
  assert.equal(r.income_breakdown.length, 0);
});

test("月指定 (month) で fiscal_period == month のみ集計", () => {
  const entries = [
    entry(1, 4, "journal", [["1010", 1000, 0], ["4010", 0, 1000]]),  // 4 月
    entry(2, 5, "journal", [["1010", 2000, 0], ["4010", 0, 2000]]),  // 5 月
    entry(3, 6, "journal", [["1010", 3000, 0], ["4010", 0, 3000]]),  // 6 月
  ];
  const r = computeProfitLoss(entries, {
    accountTypeByCode: ACCOUNTS, month: 5,
  });
  assert.equal(r.income_total, 2000);
});

test("月指定なし: 期首 (fp=0) と決算整理 (13-15) を含み、fp=16 と closing を除外", () => {
  const entries = [
    entry(1, 0, "journal", [["1010", 1000, 0], ["4010", 0, 1000]]),  // 期首
    entry(2, 5, "journal", [["1010", 2000, 0], ["4010", 0, 2000]]),  // 5 月
    entry(3, 13, "journal", [["1010", 500, 0], ["4010", 0, 500]]),   // 決算整理
    entry(4, 16, "journal", [["4010", 3000, 0], ["3020", 0, 3000]]),  // 損益振替 (除外)
    entry(5, 5, "closing", [["4010", 100, 0], ["1010", 0, 100]]),    // closing (除外)
  ];
  const r = computeProfitLoss(entries, { accountTypeByCode: ACCOUNTS });
  // 期首 1000 + 5月 2000 + 決算整理 500 = 3500
  assert.equal(r.income_total, 3500);
});

test("複数科目の breakdown", () => {
  const entries = [
    entry(1, 5, "journal", [
      ["1010", 5000, 0],
      ["4010", 0, 3000],
      ["4020", 0, 2000],
    ]),
    entry(2, 5, "journal", [
      ["5010", 1000, 0],
      ["5020", 200, 0],
      ["1010", 0, 1200],
    ]),
  ];
  const r = computeProfitLoss(entries, {
    accountTypeByCode: ACCOUNTS, accountNameByCode: NAMES,
  });
  assert.equal(r.income_breakdown.length, 2);
  assert.equal(r.expense_breakdown.length, 2);
  // ソート確認
  assert.equal(r.income_breakdown[0].account_code, "4010");
  assert.equal(r.income_breakdown[1].account_code, "4020");
  assert.equal(r.expense_breakdown[0].account_code, "5010");
  assert.equal(r.expense_breakdown[1].account_code, "5020");
  assert.equal(r.income_total, 5000);
  assert.equal(r.expense_total, 1200);
  assert.equal(r.net_income, 3800);
});

test("BS 科目 (asset/liability/equity) は P/L に含めない", () => {
  const entries = [
    entry(1, 5, "journal", [["1010", 1000, 0], ["2010", 0, 1000]]),
  ];
  const r = computeProfitLoss(entries, { accountTypeByCode: ACCOUNTS });
  assert.equal(r.income_total, 0);
  assert.equal(r.expense_total, 0);
  assert.equal(r.income_breakdown.length, 0);
  assert.equal(r.expense_breakdown.length, 0);
});

test("account_code 不明 (account マスタにない) は無視", () => {
  const entries = [
    entry(1, 5, "journal", [
      ["UNKNOWN", 500, 0],  // 未マスタ
      ["1010", 0, 500],
    ]),
  ];
  const r = computeProfitLoss(entries, { accountTypeByCode: ACCOUNTS });
  assert.equal(r.income_total, 0);
  assert.equal(r.expense_total, 0);
});

test("account_code が null の line はスキップ", () => {
  const entries = [
    {id: 1, fiscal_month: 5, is_closing: false, lines: [
      {account_code: null, debit: 100, credit: 0},   // 復号失敗
      {account_code: "5010", debit: 200, credit: 0},
    ]},
  ];
  const r = computeProfitLoss(entries, { accountTypeByCode: ACCOUNTS });
  assert.equal(r.expense_total, 200);
});

test("account_name が NAMES にない場合は code をフォールバック", () => {
  const entries = [
    entry(1, 5, "journal", [["4010", 0, 1000], ["1010", 1000, 0]]),
  ];
  const r = computeProfitLoss(entries, { accountTypeByCode: ACCOUNTS });
  assert.equal(r.income_breakdown[0].account_name, "4010");
});
