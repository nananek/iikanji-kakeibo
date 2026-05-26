// Phase E3-F-3g レビュー対応: mergeExpenses の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/medical_summary_renderer.mjs",
  import.meta.url,
);
const { mergeExpenses } = await import(M.href);


// --- helpers ---

const ACCOUNTS_META = {
  "5210": { name: "医療費", tax_category: "medical" },
  "5220": { name: "歯科", tax_category: "medical" },
  "5010": { name: "食費" },  // 非 medical
};


// --- empty ---

test("空入力で空配列", () => {
  const r = mergeExpenses([], [], ACCOUNTS_META);
  assert.deepEqual(r, []);
});

test("entries 空 + mexpense あり → amount_paid fallback", () => {
  const r = mergeExpenses([], [{
    id: 1, journal_entry_id: null, amount_paid: 500,
    patient_name: "本人", hospital_name: "X",
  }], ACCOUNTS_META);
  assert.equal(r.length, 1);
  assert.equal(r[0].amount, 500);
  assert.equal(r[0].patient_name, "本人");
  // entry がないので description は空
  assert.equal(r[0].description, "");
});


// --- entry match ---

test("journal_entry_id 一致 entry の medical 科目 debit を amount に採用", () => {
  const entries = [{
    id: 100, description: "セブン薬局", date: "2026-05-15",
    lines: [
      { account_code: "1010", debit: 0, credit: 1500 },
      { account_code: "5210", debit: 1500, credit: 0 },
    ],
  }];
  const r = mergeExpenses(entries, [{
    id: 1, journal_entry_id: 100, amount_paid: 9999,  // me.amount_paid は無視されるはず
    patient_name: "本人", hospital_name: "セブン",
  }], ACCOUNTS_META);
  assert.equal(r[0].amount, 1500);
  assert.equal(r[0].description, "セブン薬局");
  assert.equal(r[0].account_name, "医療費");
});

test("non-medical line は無視 (medicalCodes に含まれない)", () => {
  const entries = [{
    id: 100, description: "x", date: "2026-05-15",
    lines: [
      { account_code: "5010", debit: 200, credit: 0 },  // 食費 (非 medical)
      { account_code: "5210", debit: 800, credit: 0 },  // 医療費
    ],
  }];
  const r = mergeExpenses(entries, [{
    id: 1, journal_entry_id: 100, amount_paid: 0,
  }], ACCOUNTS_META);
  assert.equal(r[0].amount, 800);
  assert.equal(r[0].account_name, "医療費");
});

test("debit=0 の medical line は採用されず amount_paid fallback", () => {
  const entries = [{
    id: 100, description: "x", date: "2026-05-15",
    lines: [
      { account_code: "5210", debit: 0, credit: 1000 },  // credit のみ
    ],
  }];
  const r = mergeExpenses(entries, [{
    id: 1, journal_entry_id: 100, amount_paid: 700,
  }], ACCOUNTS_META);
  assert.equal(r[0].amount, 700);  // amount_paid fallback
  assert.equal(r[0].account_name, "");
});

test("同 entry に medical line 複数あれば先頭のみ採用", () => {
  const entries = [{
    id: 100, description: "x", date: "2026-05-15",
    lines: [
      { account_code: "5210", debit: 100, credit: 0 },
      { account_code: "5220", debit: 200, credit: 0 },
    ],
  }];
  const r = mergeExpenses(entries, [{
    id: 1, journal_entry_id: 100,
  }], ACCOUNTS_META);
  assert.equal(r[0].amount, 100);  // 先頭 (5210)
  assert.equal(r[0].account_name, "医療費");
});


// --- entry not found ---

test("journal_entry_id が entryMap にない → amount_paid fallback + description 空", () => {
  const r = mergeExpenses(
    [{ id: 999, description: "Y", lines: [] }],
    [{ id: 1, journal_entry_id: 100, amount_paid: 333 }],
    ACCOUNTS_META,
  );
  assert.equal(r[0].amount, 333);
  assert.equal(r[0].description, "");
});


// --- passthrough fields ---

test("MedicalExpense 各フィールドが view 用構造に保存される", () => {
  const r = mergeExpenses([], [{
    id: 1, journal_entry_id: null,
    amount_paid: 100,
    patient_name: "妻", hospital_name: "Y病院",
    treatment_description: "内科診療",
    provider_type: "hospital",
    insurance_reimbursement: 30,
    date: "2026-03-01",
  }], ACCOUNTS_META);
  assert.deepEqual(r[0], {
    date: "2026-03-01",
    description: "",
    amount: 100,
    account_name: "",
    patient_name: "妻",
    hospital_name: "Y病院",
    treatment_description: "内科診療",
    provider_type: "hospital",
    insurance_reimbursement: 30,
  });
});
