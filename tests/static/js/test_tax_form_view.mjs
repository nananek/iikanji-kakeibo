// Phase E3-F-3h: composeTaxFormView の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/tax_form_view.js",
  import.meta.url,
);
const { composeTaxFormView } = await import(M.href);


// --- helpers ---

function field(id, page, section, row_code, name, opts = {}) {
  return {
    id, page, section, row_code, name,
    is_subtotal: !!opts.is_subtotal,
    is_user_defined: !!opts.is_user_defined,
    display_order: opts.display_order ?? id * 10,
  };
}


// --- arg validation ---

test("amountSources が object でないと TypeError", () => {
  assert.throws(() => composeTaxFormView(null, { fields: [] }), /object/);
});

test("formStructure.fields が配列でないと TypeError", () => {
  assert.throws(
    () => composeTaxFormView({}, { fields: null }),
    /array/,
  );
});


// --- field amount mapping ---

test("P/L field: mappings の codes から pl_amounts を合計", () => {
  const v = composeTaxFormView(
    { pl_amounts: { "4010": 100000, "4020": 50000 } },
    {
      fields: [field(1, 1, "revenue", "1", "売上")],
      mappings: { 1: ["4010", "4020"] },
    },
  );
  assert.equal(v.field_data[0].amount, 150000);
  assert.equal(v.field_data[0].opening, null);
});

test("BS field: bs_opening + bs_amounts を合計", () => {
  const v = composeTaxFormView(
    {
      bs_amounts: { "1010": 80000, "1020": 20000 },
      bs_opening: { "1010": 50000, "1020": 10000 },
    },
    {
      fields: [field(1, 4, "bs_assets", "1", "現金預金")],
      mappings: { 1: ["1010", "1020"] },
    },
  );
  assert.equal(v.field_data[0].opening, 60000);
  assert.equal(v.field_data[0].amount, 100000);
});

test("mappings なし field の amount = 0", () => {
  const v = composeTaxFormView(
    { pl_amounts: {} },
    { fields: [field(1, 1, "revenue", "1", "売上")], mappings: {} },
  );
  assert.equal(v.field_data[0].amount, 0);
  assert.equal(v.field_data[0].codes.length, 0);
});


// --- display_order ---

test("fields は display_order 昇順に整列", () => {
  const v = composeTaxFormView({}, {
    fields: [
      field(1, 1, "x", "C", "C", { display_order: 30 }),
      field(2, 1, "x", "A", "A", { display_order: 10 }),
      field(3, 1, "x", "B", "B", { display_order: 20 }),
    ],
  });
  assert.deepEqual(
    v.field_data.map((d) => d.field.row_code),
    ["A", "B", "C"],
  );
});


// --- subtotals: 原価系 ---

test("cost_of_sales 小計 (row 4) = section 内非 subtotal の amount 合計", () => {
  const v = composeTaxFormView(
    { pl_amounts: { "5100": 100, "5200": 200 } },
    {
      fields: [
        field(1, 1, "cost_of_sales", "2", "期首棚卸高"),
        field(2, 1, "cost_of_sales", "3", "仕入"),
        field(3, 1, "cost_of_sales", "4", "小計", { is_subtotal: true }),
      ],
      mappings: { 1: ["5100"], 2: ["5200"] },
    },
  );
  // row 4 = section "cost_of_sales" の非 subtotal amount 合計 = 300
  const sub = v.field_data.find((d) => d.field.row_code === "4");
  assert.equal(sub.amount, 300);
});

test("cost_of_sales 差引原価 (row 6) = 小計 - 期末棚卸", () => {
  const v = composeTaxFormView(
    { pl_amounts: { "5100": 1000, "5200": 200 } },
    {
      fields: [
        field(1, 1, "cost_of_sales", "2", "期首棚卸高"),
        field(2, 1, "cost_of_sales", "5", "期末棚卸高"),
        field(3, 1, "cost_of_sales", "6", "差引原価", { is_subtotal: true }),
      ],
      mappings: { 1: ["5100"], 2: ["5200"] },
    },
  );
  // section 合計 = 1200, 期末 = 200 → 1000
  const sub = v.field_data.find((d) => d.field.row_code === "6");
  assert.equal(sub.amount, 1000);
});


// --- subtotals: 所得系 ---

test("income 差引金額 (row 31) = 売上 - 差引原価 - 経費", () => {
  const v = composeTaxFormView(
    {
      pl_amounts: {
        "4010": 1000000,  // 売上
        "5300": 200000,   // 経費
      },
    },
    {
      fields: [
        field(1, 1, "revenue", "1", "売上"),
        field(2, 1, "cost_of_sales", "6", "差引原価", { is_subtotal: true }),
        field(3, 1, "expenses", "10", "通信費"),
        field(4, 1, "expenses", "30", "経費計", { is_subtotal: true }),
        field(5, 1, "income", "31", "差引金額", { is_subtotal: true }),
      ],
      mappings: { 1: ["4010"], 3: ["5300"] },
    },
  );
  const row31 = v.field_data.find((d) => d.field.row_code === "31");
  // 1000000 - 0 - 200000 = 800000 (差引原価は subtotal で 0 のまま、cos_total=0)
  assert.equal(row31.amount, 800000);
});

test("income 所得金額 (row 37) = 控除前 (row 35) - 控除額 (row 36)", () => {
  const v = composeTaxFormView(
    { pl_amounts: {} },
    {
      fields: [
        field(1, 1, "income", "31", "差引金額", { is_subtotal: true }),
        field(2, 1, "income", "35", "控除前所得", { is_subtotal: true, display_order: 50 }),
        field(3, 1, "income", "36", "青色控除", { display_order: 60 }),
        field(4, 1, "income", "37", "所得金額", { is_subtotal: true, display_order: 70 }),
      ],
      mappings: { 3: [] },
    },
  );
  // 全部 0 なので 0
  const row37 = v.field_data.find((d) => d.field.row_code === "37");
  assert.equal(row37.amount, 0);
});


// --- BS 合計 ---

test("bs_assets AT 行 = section 合計 (期首/期末)", () => {
  const v = composeTaxFormView(
    {
      bs_amounts: { "1010": 1000, "1020": 500 },
      bs_opening: { "1010": 800, "1020": 200 },
    },
    {
      fields: [
        field(1, 4, "bs_assets", "1", "現金"),
        field(2, 4, "bs_assets", "2", "預金"),
        field(3, 4, "bs_assets", "AT", "資産合計", { is_subtotal: true }),
      ],
      mappings: { 1: ["1010"], 2: ["1020"] },
    },
  );
  const at = v.field_data.find((d) => d.field.row_code === "AT");
  assert.equal(at.amount, 1500);
  assert.equal(at.opening, 1000);
});
