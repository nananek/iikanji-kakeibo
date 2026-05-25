// Tests for the pure compareMedicalSummary helper.

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/medical_summary_validator.mjs",
  import.meta.url,
);
const { compareMedicalSummary } = await import(M.href);


test("完全一致で diffs 空", () => {
  const totals = { paid: 1000, reimbursed: 200, net: 800 };
  const patients = [
    { name: "Alice", paid: 600, reimbursed: 100, net: 500 },
    { name: "Bob", paid: 400, reimbursed: 100, net: 300 },
  ];
  const js = {
    total_paid: 1000, total_reimbursed: 200, net_total: 800,
    by_patient: [
      { name: "Alice", paid: 600, reimbursed: 100, net: 500 },
      { name: "Bob", paid: 400, reimbursed: 100, net: 300 },
    ],
  };
  assert.deepEqual(compareMedicalSummary(totals, patients, js), []);
});

test("合計値不一致は totals_mismatch", () => {
  const totals = { paid: 1000, reimbursed: 200, net: 800 };
  const js = {
    total_paid: 999, total_reimbursed: 200, net_total: 799,
    by_patient: [],
  };
  const d = compareMedicalSummary(totals, [], js);
  assert.ok(d.some((x) => x.kind === "totals_mismatch"));
  const m = d.find((x) => x.kind === "totals_mismatch");
  assert.equal(m.server.paid, 1000);
  assert.equal(m.client.paid, 999);
});

test("net だけズレていても totals_mismatch (paid - reimbursed の桁違い検知)", () => {
  const totals = { paid: 1000, reimbursed: 200, net: 800 };
  const js = {
    total_paid: 1000, total_reimbursed: 200, net_total: 799,  // net だけバグ
    by_patient: [],
  };
  const d = compareMedicalSummary(totals, [], js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "totals_mismatch");
});

test("サーバにあるが JS にない患者 → patient_missing_in_client (非ゼロのみ)", () => {
  const totals = { paid: 600, reimbursed: 100, net: 500 };
  const patients = [{ name: "Alice", paid: 600, reimbursed: 100, net: 500 }];
  const js = {
    total_paid: 600, total_reimbursed: 100, net_total: 500,
    by_patient: [],
  };
  const d = compareMedicalSummary(totals, patients, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "patient_missing_in_client");
  assert.equal(d[0].name, "Alice");
});

test("患者の金額不一致は patient_mismatch", () => {
  const totals = { paid: 600, reimbursed: 100, net: 500 };
  const patients = [{ name: "Alice", paid: 600, reimbursed: 100, net: 500 }];
  const js = {
    total_paid: 600, total_reimbursed: 100, net_total: 500,
    by_patient: [{ name: "Alice", paid: 599, reimbursed: 100, net: 499 }],
  };
  const d = compareMedicalSummary(totals, patients, js);
  // 合計は一致しているが、patient レベルが違う場合 (サーバ側集計の局所バグ)
  // 合計だけ偶然合ったケースを想定。compareMedicalSummary は両方検知する
  assert.ok(d.some((x) => x.kind === "patient_mismatch"));
});

test("JS にあるがサーバにない患者 → patient_extra_in_client (非ゼロのみ)", () => {
  const totals = { paid: 600, reimbursed: 100, net: 500 };
  const patients = [{ name: "Alice", paid: 600, reimbursed: 100, net: 500 }];
  const js = {
    total_paid: 600, total_reimbursed: 100, net_total: 500,
    by_patient: [
      { name: "Alice", paid: 600, reimbursed: 100, net: 500 },
      { name: "Ghost", paid: 50, reimbursed: 0, net: 50 },
    ],
  };
  const d = compareMedicalSummary(totals, patients, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "patient_extra_in_client");
  assert.equal(d[0].name, "Ghost");
});

test("空 (患者なし、合計ゼロ) は diffs 空", () => {
  const totals = { paid: 0, reimbursed: 0, net: 0 };
  const js = { total_paid: 0, total_reimbursed: 0, net_total: 0, by_patient: [] };
  assert.deepEqual(compareMedicalSummary(totals, [], js), []);
});
