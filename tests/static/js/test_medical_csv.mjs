// Phase E3-F-4c: 医療費集計フォーム CSV (Ver 3.1 準拠) 生成テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/medical_summary_renderer.mjs",
  import.meta.url,
);
const { buildMedicalCsv } = await import(M.href);


// --- helpers ---

function expense(opts) {
  return {
    date: opts.date || "2026-01-01",
    description: opts.description || "",
    amount: opts.amount ?? 0,
    account_name: opts.account_name || "",
    patient_name: opts.patient_name || "",
    hospital_name: opts.hospital_name || "",
    treatment_description: opts.treatment_description || "",
    provider_type: opts.provider_type || "",
    insurance_reimbursement: opts.insurance_reimbursement ?? 0,
  };
}

function viewWith(expenses) {
  return {
    totals: { paid: 0, reimbursed: 0, net: 0 },
    by_patient: [],
    expenses_list: expenses,
  };
}


// --- header ---

test("BOM + Ver 3.1 ヘッダー行が含まれる", () => {
  const csv = buildMedicalCsv(viewWith([]));
  // BOM (U+FEFF)
  assert.equal(csv.charCodeAt(0), 0xFEFF);
  // 改行は CRLF
  const lines = csv.slice(1).split("\r\n");
  assert.equal(
    lines[0],
    "医療を受けた人,病院・薬局などの名称,診療・治療,医薬品購入,介護保険サービス,その他の医療費,支払った医療費の金額,左のうち、補てんされる金額",
  );
});


// --- provider_type → 該当する 列 ---

test("provider_type=hospital は 診療・治療 に該当する", () => {
  const csv = buildMedicalCsv(viewWith([expense({
    patient_name: "本人", hospital_name: "X病院",
    provider_type: "hospital", amount: 1000,
  })]));
  // 4 列目 (index 2) が "該当する"
  const row = csv.slice(1).split("\r\n")[1].split(",");
  assert.equal(row[2], "該当する");
  assert.equal(row[3], "");
  assert.equal(row[4], "");
  assert.equal(row[5], "");
});

test("provider_type=pharmacy は 医薬品購入 に該当する", () => {
  const csv = buildMedicalCsv(viewWith([expense({
    patient_name: "本人", hospital_name: "Y薬局",
    provider_type: "pharmacy", amount: 1000,
  })]));
  const row = csv.slice(1).split("\r\n")[1].split(",");
  assert.equal(row[2], "");
  assert.equal(row[3], "該当する");
});

test("provider_type=nursing は 介護保険サービス に該当する", () => {
  const csv = buildMedicalCsv(viewWith([expense({
    provider_type: "nursing", amount: 1000,
  })]));
  const row = csv.slice(1).split("\r\n")[1].split(",");
  assert.equal(row[4], "該当する");
});

test("provider_type=other は その他の医療費 に該当する", () => {
  const csv = buildMedicalCsv(viewWith([expense({
    provider_type: "other", amount: 1000,
  })]));
  const row = csv.slice(1).split("\r\n")[1].split(",");
  assert.equal(row[5], "該当する");
});

test("provider_type 未設定 (空文字) は hospital と同じ 診療・治療 扱い", () => {
  const csv = buildMedicalCsv(viewWith([expense({
    provider_type: "", amount: 1000,
  })]));
  const row = csv.slice(1).split("\r\n")[1].split(",");
  assert.equal(row[2], "該当する");
});


// --- amount + reimbursement ---

test("amount と insurance_reimbursement が末尾 2 列に出る", () => {
  const csv = buildMedicalCsv(viewWith([expense({
    amount: 12000, insurance_reimbursement: 3000,
  })]));
  const row = csv.slice(1).split("\r\n")[1].split(",");
  assert.equal(row[6], "12000");
  assert.equal(row[7], "3000");
});

test("insurance_reimbursement=0 は空文字", () => {
  const csv = buildMedicalCsv(viewWith([expense({
    amount: 100, insurance_reimbursement: 0,
  })]));
  const row = csv.slice(1).split("\r\n")[1].split(",");
  assert.equal(row[7], "");
});


// --- CSV escape ---

test("カンマを含む値は double-quote で囲む", () => {
  const csv = buildMedicalCsv(viewWith([expense({
    patient_name: "本人,メイン", hospital_name: "X",
    amount: 100,
  })]));
  const line = csv.slice(1).split("\r\n")[1];
  assert.match(line, /^"本人,メイン",X,/);
});

test("double-quote を含む値は \"\" にエスケープ", () => {
  const csv = buildMedicalCsv(viewWith([expense({
    patient_name: 'A"B', amount: 100,
  })]));
  const line = csv.slice(1).split("\r\n")[1];
  assert.match(line, /^"A""B",/);
});

test("改行を含む値は double-quote で囲む", () => {
  const csv = buildMedicalCsv(viewWith([expense({
    hospital_name: "X\n病院", amount: 100,
  })]));
  const line = csv.slice(1).split("\r\n")[1];
  assert.match(line, /,"X\n病院",/);
});


// --- empty ---

test("expenses_list=[] でも header + 末尾 CRLF を返す", () => {
  const csv = buildMedicalCsv(viewWith([]));
  // BOM + header + CRLF
  assert.ok(csv.endsWith("\r\n"));
  // 行は 1 行 (header) のみ
  assert.equal(csv.slice(1).split("\r\n").length, 2);  // ["header", ""]
});
