// Phase E3-F-3f: composeTaxSummaryView の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/tax_summary_view.js",
  import.meta.url,
);
const { composeTaxSummaryView } = await import(M.href);


// --- arg validation ---

test("jsResult が object でないと TypeError", () => {
  assert.throws(() => composeTaxSummaryView(null), /object/);
});


// --- empty ---

test("空 jsResult で sections=[]", () => {
  const v = composeTaxSummaryView({});
  assert.deepEqual(v.sections, []);
});


// --- single category ---

test("単一 category を section に展開", () => {
  const v = composeTaxSummaryView({
    social_insurance: {
      label: "社会保険料控除",
      accounts: [{ name: "健康保険", amount: 100000 }],
      total: 100000,
    },
  });
  assert.equal(v.sections.length, 1);
  assert.deepEqual(v.sections[0], {
    code: "social_insurance",
    label: "社会保険料控除",
    total: 100000,
    accounts: [{ name: "健康保険", amount: 100000 }],
  });
});


// --- display order ---

test("既知の category は固定表示順 (社保→生保→地震→寄附→ideco→源泉) で sort", () => {
  const v = composeTaxSummaryView({
    ideco: { label: "iDeCo", accounts: [], total: 1 },
    earthquake_insurance: { label: "地震保険", accounts: [], total: 2 },
    social_insurance: { label: "社保", accounts: [], total: 3 },
    donation: { label: "寄附", accounts: [], total: 4 },
    life_insurance: { label: "生保", accounts: [], total: 5 },
    withholding_tax: { label: "源泉", accounts: [], total: 6 },
  });
  assert.deepEqual(
    v.sections.map((s) => s.code),
    ["social_insurance", "life_insurance", "earthquake_insurance",
      "donation", "ideco", "withholding_tax"],
  );
});

test("未知の category は末尾にコード昇順で追加", () => {
  const v = composeTaxSummaryView({
    z_category: { label: "未知Z", accounts: [], total: 1 },
    social_insurance: { label: "社保", accounts: [], total: 2 },
    a_category: { label: "未知A", accounts: [], total: 3 },
  });
  assert.deepEqual(
    v.sections.map((s) => s.code),
    ["social_insurance", "a_category", "z_category"],
  );
});


// --- accounts copy ---

test("accounts は slice で透過 (元配列変更の影響を受けない)", () => {
  const accounts = [{ name: "健保", amount: 100 }];
  const v = composeTaxSummaryView({
    social_insurance: { label: "社保", accounts, total: 100 },
  });
  accounts.push({ name: "X", amount: 999 });
  assert.equal(v.sections[0].accounts.length, 1);
});
