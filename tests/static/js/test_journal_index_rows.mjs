// journal/index_renderer.buildJournalRows の Node 単体テスト。
//
// 仕訳帳一覧のクライアント描画 (E3-F PR-D-4-3) の行生成・編集可否 (modifiable)
// 判定を検証する。modifiable は旧サーバ check_entry_modifiable /
// is_entry_locked_for_owner と等価。

import { test } from "node:test";
import assert from "node:assert/strict";

const MOD = new URL(
  "../../../app/static/js/journal/index_renderer.mjs",
  import.meta.url,
);
const { buildJournalRows } = await import(MOD.href);


const META = {
  "5010": { name: "食費" },
  "5020": { name: "交際費" },
  "1010": { name: "現金" },
  "1020": { name: "普通預金" },
};

function entry(o) {
  return {
    id: o.id,
    entry_number: o.entry_number,
    date: o.date,
    description: o.description || "",
    source: o.source || "journal",
    fiscal_year: o.fiscal_year ?? (o.date ? Number(o.date.slice(0, 4)) : null),
    fiscal_month: o.fiscal_month ?? null,
    fiscal_period: o.fiscal_period ?? null,
    is_closing: o.is_closing || false,
    vouchers: o.vouchers || [],
    lines: o.lines || [
      { account_code: "5010", debit: o.amount || 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: o.amount || 100 },
    ],
  };
}

function run(journals, opts = {}) {
  return buildJournalRows(journals, META, opts);
}


test("空 journals は [] を返す", () => {
  assert.deepEqual(run([]), []);
  assert.deepEqual(run(null), []);
});

test("借方/貸方科目名・金額を組み立てる", () => {
  const rows = run([entry({ id: 1, entry_number: 1, date: "2026-02-15", amount: 1200 })]);
  assert.deepEqual(rows[0].debit_names, ["食費"]);
  assert.deepEqual(rows[0].credit_names, ["現金"]);
  assert.equal(rows[0].amount, 1200);
});

test("meta に無い科目はコードにフォールバック", () => {
  const rows = run([entry({
    id: 1, entry_number: 1, date: "2026-02-15",
    lines: [{ account_code: "9999", debit: 100, credit: 0 }, { account_code: "1010", debit: 0, credit: 100 }],
  })]);
  assert.deepEqual(rows[0].debit_names, ["9999"]);
});

test("通常仕訳は modifiable=true", () => {
  const rows = run([entry({ id: 1, entry_number: 1, date: "2026-05-15", fiscal_month: 5 })],
    { closedPeriods: { 2026: 2 } });
  assert.equal(rows[0].modifiable, true);
});

test("is_closing は modifiable=false", () => {
  const rows = run([entry({ id: 1, entry_number: 1, date: "2026-12-31", is_closing: true })]);
  assert.equal(rows[0].modifiable, false);
  assert.equal(rows[0].is_closing, true);
});

test("確定済み期間 (period <= closed_period) は modifiable=false", () => {
  const rows = run([entry({ id: 1, entry_number: 1, date: "2026-02-15", fiscal_year: 2026, fiscal_month: 2 })],
    { closedPeriods: { 2026: 2 } });
  assert.equal(rows[0].modifiable, false);
});

test("確定期間より後の月は modifiable=true", () => {
  const rows = run([entry({ id: 1, entry_number: 1, date: "2026-03-15", fiscal_year: 2026, fiscal_month: 3 })],
    { closedPeriods: { 2026: 2 } });
  assert.equal(rows[0].modifiable, true);
});

test("locked_codes に含まれる科目があれば modifiable=false", () => {
  const rows = run([entry({
    id: 1, entry_number: 1, date: "2026-05-15",
    lines: [{ account_code: "5010", debit: 100, credit: 0 }, { account_code: "1010", debit: 0, credit: 100 }],
  })], { lockedCodes: ["5010"] });
  assert.equal(rows[0].modifiable, false);
});

test("period は fiscal_month → fiscal_period → date.month の順で導出", () => {
  // fiscal_month なし・fiscal_period=2 → closed 2 で locked
  const rows = run([entry({ id: 1, entry_number: 1, date: "2026-09-15", fiscal_year: 2026, fiscal_month: null, fiscal_period: 2 })],
    { closedPeriods: { 2026: 2 } });
  assert.equal(rows[0].modifiable, false);
  // fiscal_month/period なし → date.month=9 → closed 2 で not locked
  const rows2 = run([entry({ id: 2, entry_number: 2, date: "2026-09-15", fiscal_year: 2026, fiscal_month: null, fiscal_period: null })],
    { closedPeriods: { 2026: 2 } });
  assert.equal(rows2[0].modifiable, true);
});

test("date 降順・同日は entry_number 降順でソート", () => {
  const rows = run([
    entry({ id: 1, entry_number: 1, date: "2026-02-15" }),
    entry({ id: 2, entry_number: 2, date: "2026-02-16" }),
    entry({ id: 3, entry_number: 3, date: "2026-02-15" }),
  ]);
  assert.deepEqual(rows.map((r) => r.entry_id), [2, 3, 1]);
});

test("証憑 vouchers から has_voucher / voucher_id を設定", () => {
  const rows = run([entry({
    id: 1, entry_number: 1, date: "2026-02-15", source: "ai_receipt",
    vouchers: [{ id: 77, uploaded_at: "2026-02-15T00:00:00" }],
  })]);
  assert.equal(rows[0].has_voucher, true);
  assert.equal(rows[0].voucher_id, 77);
  assert.equal(rows[0].source, "ai_receipt");
});

test("証憑なしは has_voucher=false / voucher_id=null", () => {
  const rows = run([entry({ id: 1, entry_number: 1, date: "2026-02-15" })]);
  assert.equal(rows[0].has_voucher, false);
  assert.equal(rows[0].voucher_id, null);
});
