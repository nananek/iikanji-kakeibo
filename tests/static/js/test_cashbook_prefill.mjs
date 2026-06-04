// cashbook_prefill.js (#338 PR2) の単体テスト。
//
// detectCashbookFields (純粋な 3 方向検出) / applyCashbookFields (DOM/Alpine 反映)
// / hydrateCashbookEdit (fetch+復号+skip 判定) を DI モックで検証する。
// DOM/Alpine は plain object で代替する。

import { test } from "node:test";
import assert from "node:assert/strict";

const MOD = new URL(
  "../../../app/static/js/crypto/cashbook_prefill.js",
  import.meta.url,
);
const { detectCashbookFields, applyCashbookFields, hydrateCashbookEdit } =
  await import(MOD.href);


// code→{name,type_code} メタ (旧サーバ実装の全科目検索に相当)。
const META = {
  "1010": { name: "現金", type_code: "asset" },
  "1020": { name: "普通預金", type_code: "asset" },
  "2010": { name: "未払金", type_code: "liability" },
  "4010": { name: "売上", type_code: "revenue" },
  "5010": { name: "食費", type_code: "expense" },
};


// --- detectCashbookFields ---

test("detectCashbookFields: lines が配列でない → null", () => {
  assert.equal(detectCashbookFields({ lines: null, acctMetaByCode: META }), null);
});

test("detectCashbookFields: 明細が 2 行でない → null", () => {
  assert.equal(
    detectCashbookFields({ lines: [{ account_code: "1010", debit: 100, credit: 0 }], acctMetaByCode: META }),
    null,
  );
});

test("detectCashbookFields: 借方明細が無い → null", () => {
  const lines = [
    { account_code: "1010", debit: 0, credit: 500 },
    { account_code: "1020", debit: 0, credit: 500 },
  ];
  assert.equal(detectCashbookFields({ lines, acctMetaByCode: META }), null);
});

test("detectCashbookFields: expense (借方=PL科目, 貸方=BS科目)", () => {
  // 食費 5010 (debit, PL) / 現金 1010 (credit, BS) → expense
  const lines = [
    { account_code: "5010", debit: 1500, credit: 0 },
    { account_code: "1010", debit: 0, credit: 1500 },
  ];
  assert.deepEqual(detectCashbookFields({ lines, acctMetaByCode: META }), {
    transactionType: "expense",
    paymentCode: "1010",   // 貸方 BS = 支払元
    paymentName: "現金",
    categoryCode: "5010",  // 借方 PL = 科目
    categoryName: "食費",
    amount: 1500,
  });
});

test("detectCashbookFields: income (借方=BS科目, 貸方=PL科目)", () => {
  // 現金 1010 (debit, BS) / 売上 4010 (credit, PL) → income
  const lines = [
    { account_code: "1010", debit: 3000, credit: 0 },
    { account_code: "4010", debit: 0, credit: 3000 },
  ];
  assert.deepEqual(detectCashbookFields({ lines, acctMetaByCode: META }), {
    transactionType: "income",
    paymentCode: "1010",   // 借方 BS = 入金先
    paymentName: "現金",
    categoryCode: "4010",  // 貸方 PL = 収入源
    categoryName: "売上",
    amount: 3000,
  });
});

test("detectCashbookFields: transfer (借方・貸方とも BS科目)", () => {
  // 普通預金 1020 (debit, BS) / 現金 1010 (credit, BS) → transfer
  const lines = [
    { account_code: "1020", debit: 5000, credit: 0 },
    { account_code: "1010", debit: 0, credit: 5000 },
  ];
  assert.deepEqual(detectCashbookFields({ lines, acctMetaByCode: META }), {
    transactionType: "transfer",
    paymentCode: "1010",   // 貸方 BS = 移動元
    paymentName: "現金",
    categoryCode: "1020",  // 借方 BS = 移動先
    categoryName: "普通預金",
    amount: 5000,
  });
});

test("detectCashbookFields: liability も BS として扱う (transfer)", () => {
  // 未払金 2010 (liability) で支払 → 借方 BS / 貸方 BS で transfer
  const lines = [
    { account_code: "1010", debit: 800, credit: 0 },
    { account_code: "2010", debit: 0, credit: 800 },
  ];
  const r = detectCashbookFields({ lines, acctMetaByCode: META });
  assert.equal(r.transactionType, "transfer");
});

test("detectCashbookFields: 未知の科目コードは非BS扱い、名前は空文字", () => {
  // 借方 9999 (メタ無し=非BS) / 貸方 1010 (BS) → expense, category 名は空
  const lines = [
    { account_code: "9999", debit: 700, credit: 0 },
    { account_code: "1010", debit: 0, credit: 700 },
  ];
  assert.deepEqual(detectCashbookFields({ lines, acctMetaByCode: META }), {
    transactionType: "expense",
    paymentCode: "1010",
    paymentName: "現金",
    categoryCode: "9999",
    categoryName: "",
    amount: 700,
  });
});

test("detectCashbookFields: acctMetaByCode 未指定でも例外を投げない", () => {
  const lines = [
    { account_code: "5010", debit: 100, credit: 0 },
    { account_code: "1010", debit: 0, credit: 100 },
  ];
  const r = detectCashbookFields({ lines });
  // 全科目が非BS扱いになり expense、名前は空
  assert.equal(r.transactionType, "expense");
  assert.equal(r.paymentName, "");
});

test("detectCashbookFields: amount は借方金額を整数に丸める", () => {
  const lines = [
    { account_code: "5010", debit: 199.9, credit: 0 },
    { account_code: "1010", debit: 0, credit: 199.9 },
  ];
  assert.equal(detectCashbookFields({ lines, acctMetaByCode: META }).amount, 199);
});


// --- applyCashbookFields ---

test("applyCashbookFields: fields が null なら何もしない", () => {
  const amountInput = { value: "keep" };
  applyCashbookFields({ fields: null, amountInput });
  assert.equal(amountInput.value, "keep");
});

test("applyCashbookFields: amount input と Alpine scope を更新", () => {
  const amountInput = { value: "" };
  const formEl = { _tag: "form" };
  const scope = { tab: "expense", paymentCode: "", paymentName: "", categoryCode: "", categoryName: "" };
  const alpine = { $data: (el) => (el === formEl ? scope : null) };
  applyCashbookFields({
    fields: {
      transactionType: "income", paymentCode: "1010", paymentName: "現金",
      categoryCode: "4010", categoryName: "売上", amount: 3000,
    },
    formEl, amountInput, alpine,
  });
  assert.equal(amountInput.value, "3000");
  assert.equal(scope.tab, "income");
  assert.equal(scope.paymentCode, "1010");
  assert.equal(scope.paymentName, "現金");
  assert.equal(scope.categoryCode, "4010");
  assert.equal(scope.categoryName, "売上");
});

test("applyCashbookFields: Alpine 無しでも amount input は更新される", () => {
  const amountInput = { value: "" };
  applyCashbookFields({
    fields: {
      transactionType: "expense", paymentCode: "1010", paymentName: "現金",
      categoryCode: "5010", categoryName: "食費", amount: 500,
    },
    amountInput,
  });
  assert.equal(amountInput.value, "500");
});


// --- hydrateCashbookEdit ---

function makeClientClass(hasKey) {
  let closed = false;
  class C {
    constructor(_url) {}
    async status() { return { hasKey }; }
    async decrypt() { return {}; }
    close() { closed = true; }
  }
  C._wasClosed = () => closed;
  return C;
}

test("hydrateCashbookEdit: 新規 (isEdit=false) は skip", async () => {
  const r = await hydrateCashbookEdit({ isEdit: false, entryId: 1, userId: 1 });
  assert.equal(r, null);
});

test("hydrateCashbookEdit: entryId 欠落は skip", async () => {
  const r = await hydrateCashbookEdit({ isEdit: true, entryId: null, userId: 1 });
  assert.equal(r, null);
});

test("hydrateCashbookEdit: MK ロック中 (hasKey=false) は復号せず null", async () => {
  const ClientClass = makeClientClass(false);
  let fetched = false;
  const r = await hydrateCashbookEdit({
    isEdit: true, entryId: 1, userId: 1,
    ClientClass,
    fetchEntry: async () => { fetched = true; return {}; },
  });
  assert.equal(r, null);
  assert.equal(fetched, false);
  assert.equal(ClientClass._wasClosed(), true);
});

test("hydrateCashbookEdit: MK 解除済なら fetch+検出して反映", async () => {
  const ClientClass = makeClientClass(true);
  const amountInput = { value: "" };
  const formEl = { _tag: "form" };
  const scope = { tab: "expense", paymentCode: "", paymentName: "", categoryCode: "", categoryName: "" };
  const alpine = { $data: (el) => (el === formEl ? scope : null) };
  const entry = {
    lines: [
      { account_code: "1010", debit: 3000, credit: 0 },
      { account_code: "4010", debit: 0, credit: 3000 },
    ],
  };
  const r = await hydrateCashbookEdit({
    isEdit: true, entryId: 42, userId: 9,
    acctMetaByCode: META,
    formEl, amountInput, alpine,
    ClientClass,
    fetchEntry: async ({ entryId, userId }) => {
      assert.equal(entryId, 42);
      assert.equal(userId, 9);
      return entry;
    },
  });
  assert.equal(r.transactionType, "income");
  assert.equal(scope.tab, "income");
  assert.equal(amountInput.value, "3000");
  assert.equal(ClientClass._wasClosed(), true);
});

test("hydrateCashbookEdit: 明細が検出不能 (2行でない) なら fields=null で scope 不変", async () => {
  const ClientClass = makeClientClass(true);
  const scope = { tab: "expense", paymentCode: "", paymentName: "", categoryCode: "", categoryName: "" };
  const formEl = { _tag: "form" };
  const alpine = { $data: () => scope };
  const r = await hydrateCashbookEdit({
    isEdit: true, entryId: 1, userId: 1,
    acctMetaByCode: META,
    formEl, alpine,
    ClientClass,
    fetchEntry: async () => ({ lines: [{ account_code: "1010", debit: 100, credit: 0 }] }),
  });
  assert.equal(r, null);
  assert.equal(scope.tab, "expense");
  assert.equal(ClientClass._wasClosed(), true);
});
