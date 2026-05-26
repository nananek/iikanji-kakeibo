// Phase E3-F-3a: composeTrialBalanceView の単体テスト。
//
// 入力 jsRows (account_code/debit/credit) と accountsMeta (type/normal_balance/name)
// から、科目区分でまとめた sections[] を返すことを確認。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/trial_balance_view.js",
  import.meta.url,
);
const { composeTrialBalanceView } = await import(M.href);


// --- helper ---

const META = {
  "1010": { type: "asset", normal_balance: "debit", name: "現金" },
  "1020": { type: "asset", normal_balance: "debit", name: "普通預金" },
  "2010": { type: "liability", normal_balance: "credit", name: "未払金" },
  "3010": { type: "equity", normal_balance: "credit", name: "元入金" },
  "4010": { type: "revenue", normal_balance: "credit", name: "売上" },
  "5010": { type: "expense", normal_balance: "debit", name: "消耗品費" },
};


// --- argument validation ---

test("jsRows が配列でないと TypeError", () => {
  assert.throws(() => composeTrialBalanceView(null, META), /array/);
  assert.throws(() => composeTrialBalanceView({}, META), /array/);
});

test("accountsMeta が object でないと TypeError", () => {
  assert.throws(() => composeTrialBalanceView([], null), /object/);
});


// --- empty ---

test("空 jsRows で sections=[] + grandTotal=0", () => {
  const v = composeTrialBalanceView([], META);
  assert.deepEqual(v.sections, []);
  assert.deepEqual(v.grandTotal, { debit: 0, credit: 0, is_balanced: true });
});


// --- single section ---

test("単一科目の試算表 view (asset / debit normal)", () => {
  const v = composeTrialBalanceView([
    { account_code: "1010", debit: 1000, credit: 300 },
  ], META);
  assert.equal(v.sections.length, 1);
  const s = v.sections[0];
  assert.equal(s.typeCode, "asset");
  assert.equal(s.typeName, "資産");
  assert.equal(s.rows.length, 1);
  assert.deepEqual(s.rows[0], {
    code: "1010", name: "現金",
    opening: 0, debit: 1000, credit: 300, balance: 700,
  });
  assert.deepEqual(s.subtotal, {
    opening: 0, debit: 1000, credit: 300, balance: 700,
  });
});

test("credit normal 科目は (credit - debit) で balance", () => {
  const v = composeTrialBalanceView([
    { account_code: "2010", debit: 200, credit: 500 },
  ], META);
  assert.equal(v.sections[0].rows[0].balance, 300);
});


// --- type ordering ---

test("section は asset → liability → equity → revenue → expense の順", () => {
  const v = composeTrialBalanceView([
    { account_code: "5010", debit: 500, credit: 0 },
    { account_code: "4010", debit: 0, credit: 1000 },
    { account_code: "3010", debit: 0, credit: 2000 },
    { account_code: "2010", debit: 0, credit: 300 },
    { account_code: "1010", debit: 2800, credit: 0 },
  ], META);
  assert.deepEqual(
    v.sections.map((s) => s.typeCode),
    ["asset", "liability", "equity", "revenue", "expense"],
  );
});


// --- subtotal ---

test("section の subtotal は rows の合計", () => {
  const v = composeTrialBalanceView([
    { account_code: "1010", debit: 1000, credit: 200 },
    { account_code: "1020", debit: 500, credit: 100 },
  ], META);
  const s = v.sections[0];
  assert.equal(s.rows.length, 2);
  assert.deepEqual(s.subtotal, {
    opening: 0, debit: 1500, credit: 300, balance: 1200,
  });
});


// --- row sort ---

test("section 内の行は code 昇順に整列", () => {
  const v = composeTrialBalanceView([
    { account_code: "1020", debit: 500, credit: 0 },
    { account_code: "1010", debit: 1000, credit: 0 },
  ], META);
  assert.deepEqual(
    v.sections[0].rows.map((r) => r.code),
    ["1010", "1020"],
  );
});


// --- unknown account ---

test("accountsMeta にない code は無視 (skip)", () => {
  const v = composeTrialBalanceView([
    { account_code: "9999", debit: 100, credit: 0 },
    { account_code: "1010", debit: 200, credit: 0 },
  ], META);
  assert.equal(v.sections.length, 1);
  assert.equal(v.sections[0].rows.length, 1);
  assert.equal(v.sections[0].rows[0].code, "1010");
});

test("不明な type の科目も無視", () => {
  const v = composeTrialBalanceView([
    { account_code: "9000", debit: 100, credit: 0 },
  ], { "9000": { type: "unknown_type", normal_balance: "debit", name: "?" } });
  assert.deepEqual(v.sections, []);
});


// --- name fallback ---

test("meta.name が空のとき code を fallback", () => {
  const v = composeTrialBalanceView([
    { account_code: "1010", debit: 100, credit: 0 },
  ], { "1010": { type: "asset", normal_balance: "debit", name: "" } });
  assert.equal(v.sections[0].rows[0].name, "1010");
});


// --- opening (Issue #221) ---

test("opening 未指定なら 0", () => {
  const v = composeTrialBalanceView([
    { account_code: "1010", debit: 1000, credit: 0 },
  ], META);
  assert.equal(v.sections[0].rows[0].opening, 0);
  assert.equal(v.sections[0].subtotal.opening, 0);
});

test("opening 指定で balance に加算 (debit normal)", () => {
  const v = composeTrialBalanceView([
    { account_code: "1010", debit: 1000, credit: 0 },
  ], META, { opening: { "1010": 5000 } });
  assert.equal(v.sections[0].rows[0].opening, 5000);
  // balance = 5000 + 1000 - 0 = 6000
  assert.equal(v.sections[0].rows[0].balance, 6000);
  assert.equal(v.sections[0].subtotal.opening, 5000);
});

test("opening 指定で balance に加算 (credit normal)", () => {
  const v = composeTrialBalanceView([
    { account_code: "2010", debit: 200, credit: 500 },
  ], META, { opening: { "2010": 1000 } });
  // 負債 (credit normal): 1000 + (500 - 200) = 1300
  assert.equal(v.sections[0].rows[0].balance, 1300);
});

test("opening のみの code (期中 0 だが前期繰越あり) も拾う", () => {
  const v = composeTrialBalanceView([], META, {
    opening: { "1010": 8000 },
  });
  assert.equal(v.sections.length, 1);
  assert.equal(v.sections[0].rows[0].code, "1010");
  assert.equal(v.sections[0].rows[0].balance, 8000);
});


// --- grandTotal (Issue #221) ---

test("grandTotal: 借方/貸方合計 + is_balanced", () => {
  const v = composeTrialBalanceView([
    { account_code: "1010", debit: 1000, credit: 0 },
    { account_code: "4010", debit: 0, credit: 1000 },
  ], META);
  assert.equal(v.grandTotal.debit, 1000);
  assert.equal(v.grandTotal.credit, 1000);
  assert.equal(v.grandTotal.is_balanced, true);
});

test("grandTotal: 不均衡なら is_balanced=false", () => {
  const v = composeTrialBalanceView([
    { account_code: "1010", debit: 1000, credit: 0 },
    { account_code: "4010", debit: 0, credit: 800 },
  ], META);
  assert.equal(v.grandTotal.is_balanced, false);
});
