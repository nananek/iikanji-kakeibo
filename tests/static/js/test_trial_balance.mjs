// trial_balance.js (Phase E3-C-2) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/trial_balance.js",
  import.meta.url,
);
const { computeTrialBalance, balanceOf, computeBsOpeningFromPrior } = await import(M.href);


// --- helper ---

// E3-F PR-D-6-3b: 集計は保持列 fiscal_month / is_closing を読む。
function entry(id, fp, source, lines) {
  return {
    id, fiscal_year: 2026, fiscal_month: fp, is_closing: source === "closing",
    date: "2026-05-01", description: "test",
    lines: lines.map(([code, debit, credit]) => ({
      account_code: code, debit, credit, description: "",
    })),
  };
}


// --- computeTrialBalance ---

test("空配列で空結果", () => {
  assert.deepEqual(computeTrialBalance([]), []);
});

test("entries が配列でないと TypeError", () => {
  assert.throws(() => computeTrialBalance(null), /array/);
  assert.throws(() => computeTrialBalance({}), /array/);
});

test("単一仕訳: 借方/貸方が科目別に集計される", () => {
  const result = computeTrialBalance([
    entry(1, 5, "journal", [
      ["5010", 1000, 0],
      ["1010", 0, 1000],
    ]),
  ]);
  assert.deepEqual(result, [
    { account_code: "1010", debit: 0, credit: 1000 },
    { account_code: "5010", debit: 1000, credit: 0 },
  ]);
});

test("同科目を複数仕訳で集計", () => {
  const result = computeTrialBalance([
    entry(1, 5, "journal", [["5010", 300, 0], ["1010", 0, 300]]),
    entry(2, 5, "journal", [["5010", 500, 0], ["1010", 0, 500]]),
    entry(3, 6, "journal", [["5010", 100, 0], ["1010", 0, 100]]),
  ]);
  const byCode = Object.fromEntries(result.map(r => [r.account_code, r]));
  assert.equal(byCode["5010"].debit, 900);
  assert.equal(byCode["5010"].credit, 0);
  assert.equal(byCode["1010"].credit, 900);
});

test("fiscal_period フィルタで範囲外を除外", () => {
  const entries = [
    entry(1, 0, "journal", [["3010", 1000, 0]]),  // 期首
    entry(2, 1, "journal", [["5010", 100, 0]]),   // 1 月
    entry(3, 6, "journal", [["5010", 200, 0]]),   // 6 月
    entry(4, 13, "journal", [["5010", 300, 0]]),  // 決算整理 1
  ];
  // 4-6 月だけ
  const r = computeTrialBalance(entries, {
    fiscalPeriodFrom: 4, fiscalPeriodTo: 6,
  });
  // 5010 の 6 月分 200 のみ
  assert.equal(r.length, 1);
  assert.equal(r[0].account_code, "5010");
  assert.equal(r[0].debit, 200);
});

test("fiscal_month が null/undefined なら 0 扱い", () => {
  const entries = [
    {id: 1, fiscal_month: null, is_closing: false,
     lines: [{account_code: "1010", debit: 100, credit: 0}]},
    {id: 2, fiscal_month: undefined, is_closing: false,
     lines: [{account_code: "1010", debit: 200, credit: 0}]},
  ];
  const r = computeTrialBalance(entries, {
    fiscalPeriodFrom: 0, fiscalPeriodTo: 0,
  });
  assert.equal(r[0].debit, 300);
});

test("includeClosing=false (default) で source=closing は除外", () => {
  const entries = [
    entry(1, 16, "journal", [["3020", 0, 1000]]),
    entry(2, 16, "closing", [["3020", 500, 0]]),  // 除外される
  ];
  const r = computeTrialBalance(entries, { fiscalPeriodTo: 16 });
  // closing 仕訳が除外され、純粋に journal の 0/1000
  assert.equal(r[0].account_code, "3020");
  assert.equal(r[0].debit, 0);
  assert.equal(r[0].credit, 1000);
});

test("includeClosing=true で source=closing も含める", () => {
  const entries = [
    entry(1, 16, "journal", [["3020", 0, 1000]]),
    entry(2, 16, "closing", [["3020", 500, 0]]),
  ];
  const r = computeTrialBalance(entries, {
    fiscalPeriodTo: 16, includeClosing: true,
  });
  assert.equal(r[0].debit, 500);
  assert.equal(r[0].credit, 1000);
});

test("account_code が null の line はスキップ (復号失敗のフォールバック対応)", () => {
  const entries = [
    {id: 1, fiscal_month: 5, is_closing: false,
     lines: [
       {account_code: null, debit: 100, credit: 0},  // 復号失敗
       {account_code: "5010", debit: 200, credit: 0},
     ]},
  ];
  const r = computeTrialBalance(entries);
  assert.equal(r.length, 1);
  assert.equal(r[0].account_code, "5010");
  assert.equal(r[0].debit, 200);
});

test("結果は account_code でソート済", () => {
  const entries = [
    entry(1, 5, "journal", [
      ["9999", 100, 0], ["1010", 0, 100], ["5020", 50, 0], ["5010", 50, 0],
    ]),
  ];
  const r = computeTrialBalance(entries);
  assert.deepEqual(
    r.map(x => x.account_code),
    ["1010", "5010", "5020", "9999"],
  );
});


// --- balanceOf ---

test("balanceOf: debit normal", () => {
  // 資産 = 借方残 = debit - credit
  assert.equal(balanceOf({debit: 1000, credit: 300}, "debit"), 700);
  assert.equal(balanceOf({debit: 100, credit: 500}, "debit"), -400);
});

test("balanceOf: credit normal", () => {
  // 負債 / 純資産 / 収益 = 貸方残 = credit - debit
  assert.equal(balanceOf({debit: 200, credit: 500}, "credit"), 300);
  assert.equal(balanceOf({debit: 500, credit: 200}, "credit"), -300);
});

test("balanceOf: unsupported normalBalance で throw", () => {
  assert.throws(() => balanceOf({debit: 100, credit: 50}, "evil"), /unsupported/);
});


// --- computeBsOpeningFromPrior (B/S 前年繰越) ---

const META = {
  "1010": { type: "asset", normal_balance: "debit" },     // 現金
  "2010": { type: "liability", normal_balance: "credit" }, // 買掛
  "3020": { type: "equity", normal_balance: "credit" },    // 繰越利益
  "4010": { type: "revenue", normal_balance: "credit" },   // 売上 (P/L)
  "5010": { type: "expense", normal_balance: "debit" },    // 費用 (P/L)
};

function ent(lines) {
  return { lines: lines.map(([c, d, cr]) => ({ account_code: c, debit: d, credit: cr })) };
}

test("computeBsOpeningFromPrior: B/S 科目だけ符号付きで繰り越す", () => {
  const prior = [
    ent([["1010", 100000, 0], ["2010", 0, 40000], ["3020", 0, 60000]]),
    ent([["1010", 0, 30000], ["2010", 10000, 0]]),
  ];
  const o = computeBsOpeningFromPrior(prior, META);
  assert.equal(o["1010"], 70000);   // 資産: 100000-30000
  assert.equal(o["2010"], 30000);   // 負債(credit): 40000-10000
  assert.equal(o["3020"], 60000);   // 純資産(credit): 60000-0
});

test("computeBsOpeningFromPrior: P/L 科目は繰り越さない", () => {
  const prior = [ent([["4010", 0, 500000], ["5010", 200000, 0], ["1010", 300000, 0]])];
  const o = computeBsOpeningFromPrior(prior, META);
  assert.equal(o["1010"], 300000);
  assert.equal("4010" in o, false);  // 収益は対象外
  assert.equal("5010" in o, false);  // 費用は対象外
});

test("computeBsOpeningFromPrior: 空/未知科目は無視", () => {
  assert.deepEqual(computeBsOpeningFromPrior([], META), {});
  const o = computeBsOpeningFromPrior([ent([["9999", 100, 0], [null, 50, 0]])], META);
  assert.deepEqual(o, {});  // META に無い 9999, account_code null は無視
});
