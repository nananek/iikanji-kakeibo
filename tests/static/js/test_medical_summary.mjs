// medical_summary.js (Phase E3-C-8) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/medical_summary.js",
  import.meta.url,
);
const { computeMedicalSummary } = await import(M.href);


function exp(p) {
  return {
    date: p.date ?? "2026-05-01",
    description: p.description ?? "",
    amount: p.amount ?? 0,
    account_name: p.account_name ?? "医療費",
    patient_name: p.patient_name ?? "",
    hospital_name: p.hospital_name ?? "",
    treatment_description: p.treatment_description ?? "",
    provider_type: p.provider_type ?? "",
    insurance_reimbursement: p.insurance_reimbursement ?? 0,
  };
}


test("空配列で全 0", () => {
  const r = computeMedicalSummary([]);
  assert.equal(r.total_paid, 0);
  assert.equal(r.total_reimbursed, 0);
  assert.equal(r.net_total, 0);
  assert.deepEqual(r.by_patient, []);
});

test("array でないと TypeError", () => {
  assert.throws(() => computeMedicalSummary(null), /array/);
});

test("単一支払: total と net 計算", () => {
  const r = computeMedicalSummary([
    exp({patient_name: "本人", hospital_name: "A病院",
         amount: 10000, insurance_reimbursement: 3000,
         provider_type: "hospital"}),
  ]);
  assert.equal(r.total_paid, 10000);
  assert.equal(r.total_reimbursed, 3000);
  assert.equal(r.net_total, 7000);
  assert.equal(r.by_patient.length, 1);
  assert.equal(r.by_patient[0].name, "本人");
  assert.equal(r.by_patient[0].paid, 10000);
  assert.equal(r.by_patient[0].net, 7000);
  assert.equal(r.by_patient[0].hospitals[0].name, "A病院");
  assert.equal(r.by_patient[0].hospitals[0].provider_type, "hospital");
});

test("受診者別 + 病院別の階層集計", () => {
  const r = computeMedicalSummary([
    exp({patient_name: "本人", hospital_name: "A病院", amount: 5000}),
    exp({patient_name: "本人", hospital_name: "A病院", amount: 3000,
         insurance_reimbursement: 1000}),
    exp({patient_name: "本人", hospital_name: "B薬局", amount: 2000,
         provider_type: "pharmacy"}),
    exp({patient_name: "家族", hospital_name: "A病院", amount: 1500}),
  ]);
  // 本人 = 10000、家族 = 1500
  assert.equal(r.by_patient[0].name, "本人");
  assert.equal(r.by_patient[0].paid, 10000);
  assert.equal(r.by_patient[0].reimbursed, 1000);
  assert.equal(r.by_patient[0].net, 9000);
  assert.equal(r.by_patient[0].hospitals[0].name, "A病院");
  assert.equal(r.by_patient[0].hospitals[0].paid, 8000);
  assert.equal(r.by_patient[0].hospitals[0].reimbursed, 1000);
  assert.equal(r.by_patient[0].hospitals[1].name, "B薬局");
  assert.equal(r.by_patient[0].hospitals[1].paid, 2000);
  assert.equal(r.by_patient[0].hospitals[1].provider_type, "pharmacy");
  assert.equal(r.by_patient[1].name, "家族");
  assert.equal(r.by_patient[1].paid, 1500);
});

test("受診者別ソート: paid 降順", () => {
  const r = computeMedicalSummary([
    exp({patient_name: "C", amount: 100}),
    exp({patient_name: "A", amount: 1000}),
    exp({patient_name: "B", amount: 500}),
  ]);
  assert.deepEqual(r.by_patient.map(p => p.name), ["A", "B", "C"]);
});

test("病院別ソート: paid 降順", () => {
  const r = computeMedicalSummary([
    exp({patient_name: "本人", hospital_name: "C", amount: 100}),
    exp({patient_name: "本人", hospital_name: "A", amount: 1000}),
    exp({patient_name: "本人", hospital_name: "B", amount: 500}),
  ]);
  const hs = r.by_patient[0].hospitals;
  assert.deepEqual(hs.map(h => h.name), ["A", "B", "C"]);
});

test("受診者名・病院名が空文字なら '(未設定)' に集約", () => {
  const r = computeMedicalSummary([
    exp({patient_name: "", hospital_name: "", amount: 500}),
    exp({patient_name: "", hospital_name: "", amount: 300}),
    exp({patient_name: "本人", hospital_name: "A", amount: 1000}),
  ]);
  // 本人 1000 → 1 位、未設定 800 → 2 位
  assert.equal(r.by_patient[0].name, "本人");
  assert.equal(r.by_patient[1].name, "(未設定)");
  assert.equal(r.by_patient[1].paid, 800);
  assert.equal(r.by_patient[1].hospitals[0].name, "(未設定)");
});

test("insurance_reimbursement が欠落しているフィールドは 0 扱い", () => {
  const r = computeMedicalSummary([
    exp({patient_name: "本人", amount: 5000}),  // insurance_reimbursement=0
  ]);
  assert.equal(r.total_reimbursed, 0);
  assert.equal(r.net_total, 5000);
});

test("provider_type が複数: 最初に見た値が保持される", () => {
  // 同じ病院で provider_type が違う場合 (実用上少ないが防御的)
  const r = computeMedicalSummary([
    exp({patient_name: "本人", hospital_name: "X",
         amount: 1000, provider_type: "hospital"}),
    exp({patient_name: "本人", hospital_name: "X",
         amount: 2000, provider_type: "pharmacy"}),
  ]);
  // 集計は paid 合算、provider_type は最初の方を保持
  assert.equal(r.by_patient[0].hospitals[0].paid, 3000);
  assert.equal(r.by_patient[0].hospitals[0].provider_type, "hospital");
});

test("amount が undefined / 欠落でも 0 扱いで集計", () => {
  const r = computeMedicalSummary([
    {patient_name: "本人", hospital_name: "A"},  // amount なし
    exp({patient_name: "本人", hospital_name: "A", amount: 500}),
  ]);
  assert.equal(r.by_patient[0].paid, 500);
});
