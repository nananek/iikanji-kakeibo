// medical/index_renderer.mjs の buildMedicalRows (純粋関数) の単体テスト。
// DOM 描画ロジックは対象外 (document ガードで auto-run しない)。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/medical/index_renderer.mjs",
  import.meta.url,
);
const { buildMedicalRows } = await import(M.href);


const ACCOUNTS_META = {
  "6010": { name: "医療費", tax_category: "medical" },
  "1010": { name: "現金", tax_category: null },
  "5010": { name: "消耗品費", tax_category: null },
};


function entry(id, dateStr, description, lines) {
  return { id, date: dateStr, description, lines };
}


test("医療費科目の借方明細 1 つ = 1 行、me 詳細を紐付け", () => {
  const journals = [
    entry(10, "2026-05-01", "医療費: A病院", [
      { account_code: "6010", debit: 5000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 5000 },
    ]),
  ];
  const mexpenses = [
    {
      id: 99, journal_entry_id: 10, date: "2026-05-01",
      patient_name: "本人", hospital_name: "A病院",
      treatment_description: "内科", provider_type: "hospital",
      amount_paid: 5000, insurance_reimbursement: 1000,
    },
  ];
  const rows = buildMedicalRows(journals, mexpenses, ACCOUNTS_META);
  assert.equal(rows.length, 1);
  const r = rows[0];
  assert.equal(r.entry_id, 10);
  assert.equal(r.medical_expense_id, 99);
  assert.equal(r.amount, 5000);
  assert.equal(r.account_name, "医療費");
  assert.equal(r.description, "医療費: A病院");
  assert.equal(r.patient_name, "本人");
  assert.equal(r.provider_type, "hospital");
  assert.equal(r.insurance_reimbursement, 1000);
});

test("me が無い仕訳でも行は出る (詳細は空)", () => {
  const journals = [
    entry(11, "2026-04-01", "医療費", [
      { account_code: "6010", debit: 3000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 3000 },
    ]),
  ];
  const rows = buildMedicalRows(journals, [], ACCOUNTS_META);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].medical_expense_id, null);
  assert.equal(rows[0].patient_name, "");
  assert.equal(rows[0].amount, 3000);
});

test("医療費科目を含まない仕訳は行を出さない", () => {
  const journals = [
    entry(12, "2026-03-01", "文具", [
      { account_code: "5010", debit: 200, credit: 0 },
      { account_code: "1010", debit: 0, credit: 200 },
    ]),
  ];
  const rows = buildMedicalRows(journals, [], ACCOUNTS_META);
  assert.equal(rows.length, 0);
});

test("医療費科目でも貸方明細は行を出さない", () => {
  const journals = [
    entry(13, "2026-03-01", "返金", [
      { account_code: "1010", debit: 500, credit: 0 },
      { account_code: "6010", debit: 0, credit: 500 },
    ]),
  ];
  const rows = buildMedicalRows(journals, [], ACCOUNTS_META);
  assert.equal(rows.length, 0);
});

test("日付降順でソート", () => {
  const journals = [
    entry(20, "2026-01-10", "古い", [
      { account_code: "6010", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 100 },
    ]),
    entry(21, "2026-06-20", "新しい", [
      { account_code: "6010", debit: 200, credit: 0 },
      { account_code: "1010", debit: 0, credit: 200 },
    ]),
  ];
  const rows = buildMedicalRows(journals, [], ACCOUNTS_META);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].entry_id, 21); // 新しい方が先頭
  assert.equal(rows[1].entry_id, 20);
});

test("1 仕訳に医療費科目の借方が複数あれば複数行", () => {
  const journals = [
    entry(30, "2026-05-01", "複数", [
      { account_code: "6010", debit: 1000, credit: 0 },
      { account_code: "6010", debit: 2000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 3000 },
    ]),
  ];
  const rows = buildMedicalRows(journals, [], ACCOUNTS_META);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].amount + rows[1].amount, 3000);
});

test("空入力で空配列、null セーフ", () => {
  assert.deepEqual(buildMedicalRows([], [], {}), []);
  assert.deepEqual(buildMedicalRows(null, null, null), []);
});
