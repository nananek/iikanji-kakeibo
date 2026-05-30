// ledger_context.buildPaymentLedgerContext の Node 単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";


const MOD = new URL(
  "../../../app/static/js/crypto/ledger_context.js",
  import.meta.url,
);
const { buildPaymentLedgerContext } = await import(MOD.href);


const NAME_MAP = { "1010": "現金", "5010": "食費", "5020": "住居費" };

// 復号済み仕訳 (fetchJournalsForYear 正規化形式) のサンプル。
// 1010 (現金) で 食費 を支払った仕訳 (借方 食費 / 貸方 現金)。
function cashExpense({ id, date, description, code, amount }) {
  return {
    id, date, description,
    lines: [
      { account_code: code, debit: amount, credit: 0 },
      { account_code: "1010", debit: 0, credit: amount },
    ],
  };
}


test("account が無ければ空文字 (paymentAccountCode falsy)", () => {
  assert.equal(
    buildPaymentLedgerContext({
      accountName: "現金", paymentAccountCode: "",
      journalEntries: [], accountNameMap: NAME_MAP,
    }),
    "",
  );
});

test("journalEntries 非配列で throw", () => {
  assert.throws(
    () => buildPaymentLedgerContext({
      accountName: "現金", paymentAccountCode: "1010",
      journalEntries: null, accountNameMap: NAME_MAP,
    }),
    /journalEntries must be an array/,
  );
});

test("対象口座の明細が無ければ (元帳データなし)", () => {
  // 5010 の明細しか無く 1010 を含まない仕訳
  const entries = [{
    id: 1, date: "2026-02-01", description: "x",
    lines: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "5020", debit: 0, credit: 100 },
    ],
  }];
  assert.equal(
    buildPaymentLedgerContext({
      accountName: "現金", paymentAccountCode: "1010",
      journalEntries: entries, accountNameMap: NAME_MAP,
    }),
    "(元帳データなし)",
  );
});

test("ヘッダ + 行の整形 (相手科目名 / 入金 / 出金)", () => {
  const entries = [
    cashExpense({ id: 1, date: "2026-02-15", description: "セブン", code: "5010", amount: 500 }),
  ];
  const text = buildPaymentLedgerContext({
    accountName: "現金", paymentAccountCode: "1010",
    journalEntries: entries, accountNameMap: NAME_MAP,
  });
  const lines = text.split("\n");
  assert.equal(lines[0], "【現金】の元帳（直近1件）");
  assert.equal(lines[1], "日付 | 摘要 | 相手科目 | 入金 | 出金");
  assert.equal(lines[2], "-".repeat(60));
  // 現金は貸方 (credit=500) → 出金 ¥500、入金は -
  assert.equal(lines[3], "2026-02-15 | セブン | 食費 | - | ¥500");
});

test("入金側 (現金が借方) の整形", () => {
  // 給与: 借方 現金 / 貸方 (収益)。現金 line は debit=300000
  const entries = [{
    id: 2, date: "2026-02-25", description: "給与",
    lines: [
      { account_code: "1010", debit: 300000, credit: 0 },
      { account_code: "5020", debit: 0, credit: 300000 },
    ],
  }];
  const text = buildPaymentLedgerContext({
    accountName: "現金", paymentAccountCode: "1010",
    journalEntries: entries, accountNameMap: NAME_MAP,
  });
  const lines = text.split("\n");
  // 入金 ¥300,000 (カンマ区切り)、出金 -
  assert.equal(lines[3], "2026-02-25 | 給与 | 住居費 | ¥300,000 | -");
});

test("date desc, id desc でソート", () => {
  const entries = [
    cashExpense({ id: 1, date: "2026-01-01", description: "古い", code: "5010", amount: 100 }),
    cashExpense({ id: 3, date: "2026-03-01", description: "新しい", code: "5010", amount: 300 }),
    cashExpense({ id: 2, date: "2026-03-01", description: "同日後", code: "5010", amount: 200 }),
  ];
  const text = buildPaymentLedgerContext({
    accountName: "現金", paymentAccountCode: "1010",
    journalEntries: entries, accountNameMap: NAME_MAP,
  });
  const lines = text.split("\n").slice(3); // データ行のみ
  // date desc → 2026-03-01 が先。同日は id desc → id3 (新しい) → id2 (同日後) → id1 (古い)
  assert.match(lines[0], /新しい/);
  assert.match(lines[1], /同日後/);
  assert.match(lines[2], /古い/);
});

test("limit で件数を切る", () => {
  const entries = [];
  for (let i = 1; i <= 5; i++) {
    entries.push(cashExpense({
      id: i, date: `2026-02-0${i}`, description: `e${i}`, code: "5010", amount: i * 100,
    }));
  }
  const text = buildPaymentLedgerContext({
    accountName: "現金", paymentAccountCode: "1010",
    journalEntries: entries, accountNameMap: NAME_MAP, limit: 2,
  });
  assert.match(text, /直近2件/);
  const dataLines = text.split("\n").slice(3);
  assert.equal(dataLines.length, 2);
});

test("相手科目が name_map に無ければ ?", () => {
  const entries = [
    cashExpense({ id: 1, date: "2026-02-15", description: "謎", code: "9999", amount: 500 }),
  ];
  const text = buildPaymentLedgerContext({
    accountName: "現金", paymentAccountCode: "1010",
    journalEntries: entries, accountNameMap: NAME_MAP,
  });
  assert.match(text.split("\n")[3], /\| \? \|/);
});

test("複数の相手科目を ', ' 連結", () => {
  // 借方: 食費 + 住居費、貸方: 現金 (合算)
  const entries = [{
    id: 1, date: "2026-02-15", description: "まとめ買い",
    lines: [
      { account_code: "5010", debit: 300, credit: 0 },
      { account_code: "5020", debit: 200, credit: 0 },
      { account_code: "1010", debit: 0, credit: 500 },
    ],
  }];
  const text = buildPaymentLedgerContext({
    accountName: "現金", paymentAccountCode: "1010",
    journalEntries: entries, accountNameMap: NAME_MAP,
  });
  assert.match(text.split("\n")[3], /食費, 住居費/);
});
