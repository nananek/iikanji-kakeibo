// crypto/suggest_categories_classical.suggestCategoriesByHistory の Node 単体テスト。
//
// サーバ tests/test_journal_views_extra.py::TestSuggestCategories (撤去済) と
// 同等のケースを移植し、「同一摘要の最新仕訳の相手科目」推定の振る舞いを検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const MOD = new URL(
  "../../../app/static/js/crypto/suggest_categories_classical.js",
  import.meta.url,
);
const { suggestCategoriesByHistory } = await import(MOD.href);


const PAYMENT = "1010";
const NAMES = { "5010": "食費", "5020": "交際費", "1010": "現金", "1020": "普通預金" };

let _seq = 0;
function entry({ date, debitCode, creditCode, amount = 100, description = "", id }) {
  return {
    id: id ?? ++_seq,
    date,
    description,
    source: "cashbook",
    lines: [
      { account_code: debitCode, debit: amount, credit: 0 },
      { account_code: creditCode, debit: 0, credit: amount },
    ],
  };
}

function run(descriptions, journalEntries, opts = {}) {
  return suggestCategoriesByHistory({
    descriptions,
    paymentAccountCode: opts.paymentAccountCode ?? PAYMENT,
    journalEntries,
    accountNameMap: opts.accountNameMap ?? NAMES,
  });
}


test("空 descriptions は {} を返す", () => {
  assert.deepEqual(run([], []), {});
});

test("空文字のみの descriptions は {} を返す", () => {
  assert.deepEqual(run(["", "", ""], [entry({ date: "2026-01-15", debitCode: "5010", creditCode: PAYMENT, description: "" })]), {});
});

test("同一摘要の仕訳から相手科目を返す (支払口座以外)", () => {
  const entries = [
    entry({ date: "2026-01-15", debitCode: "5010", creditCode: PAYMENT, description: "ファミマ" }),
  ];
  const r = run(["ファミマ"], entries);
  assert.deepEqual(r["ファミマ"], { account_code: "5010", account_name: "食費" });
});

test("マッチ無しの摘要は結果に含まれない", () => {
  const r = run(["未知の摘要"], [entry({ date: "2026-01-15", debitCode: "5010", creditCode: PAYMENT, description: "ファミマ" })]);
  assert.equal("未知の摘要" in r, false);
});

test("最新 (date desc) の仕訳が採用される", () => {
  const entries = [
    entry({ id: 1, date: "2025-03-01", debitCode: "5010", creditCode: PAYMENT, description: "光熱費" }),
    entry({ id: 2, date: "2026-02-01", debitCode: "5020", creditCode: PAYMENT, description: "光熱費" }),
  ];
  const r = run(["光熱費"], entries);
  assert.equal(r["光熱費"].account_code, "5020"); // 新しい方
});

test("同日付なら id desc で最新を採用", () => {
  const entries = [
    entry({ id: 10, date: "2026-02-01", debitCode: "5010", creditCode: PAYMENT, description: "X" }),
    entry({ id: 20, date: "2026-02-01", debitCode: "5020", creditCode: PAYMENT, description: "X" }),
  ];
  const r = run(["X"], entries);
  assert.equal(r["X"].account_code, "5020"); // id 大きい方
});

test("支払口座コードは相手科目から除外される", () => {
  // 借方が支払口座、貸方が相手科目のケース (返金など)
  const entries = [
    entry({ date: "2026-01-15", debitCode: PAYMENT, creditCode: "5010", description: "返金" }),
  ];
  const r = run(["返金"], entries);
  assert.deepEqual(r["返金"], { account_code: "5010", account_name: "食費" });
});

test("無効科目 (nameMap 不在) はスキップし次の明細を見る", () => {
  // 5099 は nameMap に無い (= 無効科目扱い) ので 3 行目の 5010 が採用される
  const e = {
    id: 1, date: "2026-01-15", description: "複数明細", source: "journal",
    lines: [
      { account_code: PAYMENT, debit: 0, credit: 300 },
      { account_code: "5099", debit: 100, credit: 0 }, // 無効
      { account_code: "5010", debit: 200, credit: 0 }, // 有効
    ],
  };
  const r = run(["複数明細"], [e]);
  assert.deepEqual(r["複数明細"], { account_code: "5010", account_name: "食費" });
});

test("全明細が無効/支払口座なら結果に含まれない", () => {
  const e = {
    id: 1, date: "2026-01-15", description: "全無効", source: "journal",
    lines: [
      { account_code: PAYMENT, debit: 0, credit: 100 },
      { account_code: "9999", debit: 100, credit: 0 }, // 無効
    ],
  };
  const r = run(["全無効"], [e]);
  assert.equal("全無効" in r, false);
});

test("複数摘要を一括推定 / 重複摘要は 1 度だけ", () => {
  const entries = [
    entry({ date: "2026-01-10", debitCode: "5010", creditCode: PAYMENT, description: "A" }),
    entry({ date: "2026-01-11", debitCode: "5020", creditCode: PAYMENT, description: "B" }),
  ];
  const r = run(["A", "B", "A"], entries);
  assert.equal(r["A"].account_code, "5010");
  assert.equal(r["B"].account_code, "5020");
  assert.equal(Object.keys(r).length, 2);
});

test("paymentAccountCode が falsy なら除外せず最初の有効科目を返す", () => {
  const entries = [
    entry({ date: "2026-01-15", debitCode: "1020", creditCode: "5010", description: "振替" }),
  ];
  const r = run(["振替"], entries, { paymentAccountCode: "" });
  assert.equal(r["振替"].account_code, "1020"); // 1 行目 (借方) が採用される
});

test("date 欠落の仕訳は最後尾に送られる", () => {
  const entries = [
    { id: 1, date: null, description: "Z", lines: [{ account_code: "5020", debit: 100, credit: 0 }] },
    entry({ id: 2, date: "2026-01-01", debitCode: "5010", creditCode: PAYMENT, description: "Z" }),
  ];
  const r = run(["Z"], entries);
  assert.equal(r["Z"].account_code, "5010"); // date 有りが最新扱い
});

test("非配列入力は例外", () => {
  assert.throws(() => suggestCategoriesByHistory({ descriptions: null, journalEntries: [] }));
  assert.throws(() => suggestCategoriesByHistory({ descriptions: [], journalEntries: null }));
});
