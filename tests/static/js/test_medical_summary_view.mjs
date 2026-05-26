// Phase E3-F-3g: composeMedicalSummaryView の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/medical_summary_view.js",
  import.meta.url,
);
const { composeMedicalSummaryView } = await import(M.href);


// --- arg validation ---

test("computeResult が object でないと TypeError", () => {
  assert.throws(() => composeMedicalSummaryView(null), /object/);
});

test("expenses が配列でないと TypeError", () => {
  assert.throws(
    () => composeMedicalSummaryView({}, null),
    /array/,
  );
});


// --- totals ---

test("computeResult の合計を totals に展開", () => {
  const v = composeMedicalSummaryView({
    total_paid: 1500,
    total_reimbursed: 200,
    net_total: 1300,
    by_patient: [],
  }, []);
  assert.deepEqual(v.totals, { paid: 1500, reimbursed: 200, net: 1300 });
});

test("computeResult.by_patient を slice で透過", () => {
  const by = [{ name: "A", paid: 100, reimbursed: 0, net: 100, hospitals: [] }];
  const v = composeMedicalSummaryView({
    total_paid: 100, total_reimbursed: 0, net_total: 100, by_patient: by,
  }, []);
  by.push({ name: "B" });
  assert.equal(v.by_patient.length, 1);
});


// --- expenses_list ordering ---

test("expenses は date 昇順に整列", () => {
  const v = composeMedicalSummaryView({
    total_paid: 0, total_reimbursed: 0, net_total: 0, by_patient: [],
  }, [
    { date: "2026-05-15", description: "B" },
    { date: "2026-01-10", description: "A" },
    { date: "2026-08-20", description: "C" },
  ]);
  assert.deepEqual(
    v.expenses_list.map((e) => e.description),
    ["A", "B", "C"],
  );
});

test("date が null の expense は先頭側 (空文字相当)", () => {
  const v = composeMedicalSummaryView({
    total_paid: 0, total_reimbursed: 0, net_total: 0, by_patient: [],
  }, [
    { date: "2026-05-15", description: "B" },
    { date: null, description: "(no date)" },
  ]);
  // null は "" になるため先頭
  assert.equal(v.expenses_list[0].description, "(no date)");
});


// --- empty ---

test("空入力で空 view", () => {
  const v = composeMedicalSummaryView({
    total_paid: 0, total_reimbursed: 0, net_total: 0, by_patient: [],
  }, []);
  assert.deepEqual(v.totals, { paid: 0, reimbursed: 0, net: 0 });
  assert.equal(v.by_patient.length, 0);
  assert.equal(v.expenses_list.length, 0);
});


// --- expenses copy ---

test("expenses は slice で透過", () => {
  const expenses = [{ date: "2026-01-01", description: "A" }];
  const v = composeMedicalSummaryView({
    total_paid: 0, total_reimbursed: 0, net_total: 0, by_patient: [],
  }, expenses);
  expenses.push({ date: "2026-02-01", description: "B" });
  assert.equal(v.expenses_list.length, 1);
});


// --- computeResult のフィールド欠落 fallback ---

test("computeResult の total フィールドが欠落していても 0 fallback", () => {
  const v = composeMedicalSummaryView({ by_patient: [] }, []);
  assert.deepEqual(v.totals, { paid: 0, reimbursed: 0, net: 0 });
});
