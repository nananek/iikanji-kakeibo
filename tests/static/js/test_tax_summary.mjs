// tax_summary.js (Phase E3-C-6) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/reports/tax_summary.js",
  import.meta.url,
);
const { computeTaxSummary, TAX_CATEGORY_LABELS } = await import(M.href);


const TAX_CAT = {
  "5050": "social_insurance",   // 健康保険料
  "5051": "social_insurance",   // 国民年金
  "5060": "life_insurance",     // 生命保険料
  "5070": "donation",
  "5099": "medical",            // 除外対象
  "5100": "resident_tax",       // 除外対象
  "5200": null,                 // 対象外
  "5010": null,
};
const NAMES = {
  "5050": "健康保険料", "5051": "国民年金", "5060": "生命保険料",
  "5070": "ふるさと納税", "5099": "医療費",
};

function entry(id, source, lines) {
  return {
    id, fiscal_period: 5, source,
    lines: lines.map(([code, debit, credit]) => ({
      account_code: code, debit, credit,
    })),
  };
}


test("空配列で空 dict", () => {
  const r = computeTaxSummary([], { taxCategoryByCode: TAX_CAT });
  assert.deepEqual(r, {});
});

test("options バリデーション", () => {
  assert.throws(() => computeTaxSummary([], {}), /taxCategoryByCode/);
  assert.throws(() => computeTaxSummary(null, { taxCategoryByCode: {} }), /array/);
});

test("基本: tax_category 別に集計", () => {
  const entries = [
    entry(1, "journal", [["5050", 30000, 0], ["1010", 0, 30000]]),
    entry(2, "journal", [["5051", 15000, 0], ["1010", 0, 15000]]),
    entry(3, "journal", [["5060", 60000, 0], ["1010", 0, 60000]]),
  ];
  const r = computeTaxSummary(entries, {
    taxCategoryByCode: TAX_CAT, accountNameByCode: NAMES,
  });
  // 社会保険料控除 = 健康 30000 + 年金 15000 = 45000
  assert.equal(r.social_insurance.total, 45000);
  assert.equal(r.social_insurance.label, "社会保険料控除");
  assert.equal(r.social_insurance.accounts.length, 2);
  // accounts ソート (name 昇順): 健康保険料 / 国民年金
  assert.equal(r.social_insurance.accounts[0].name, "健康保険料");
  assert.equal(r.social_insurance.accounts[1].name, "国民年金");
  // 生命保険料控除 60000
  assert.equal(r.life_insurance.total, 60000);
});

test("medical / resident_tax は除外", () => {
  const entries = [
    entry(1, "journal", [["5099", 100000, 0], ["1010", 0, 100000]]),  // 医療
    entry(2, "journal", [["5100", 50000, 0], ["1010", 0, 50000]]),    // 住民税
    entry(3, "journal", [["5050", 30000, 0], ["1010", 0, 30000]]),
  ];
  const r = computeTaxSummary(entries, { taxCategoryByCode: TAX_CAT });
  assert.equal(r.medical, undefined);
  assert.equal(r.resident_tax, undefined);
  assert.equal(r.social_insurance.total, 30000);
});

test("source='closing' は除外", () => {
  const entries = [
    entry(1, "journal", [["5050", 30000, 0]]),
    entry(2, "closing", [["5050", 5000, 0]]),  // 除外
  ];
  const r = computeTaxSummary(entries, { taxCategoryByCode: TAX_CAT });
  assert.equal(r.social_insurance.total, 30000);
});

test("debit - credit (返金 credit で減算)", () => {
  const entries = [
    entry(1, "journal", [["5060", 12000, 0], ["1010", 0, 12000]]),
    entry(2, "journal", [["1010", 2000, 0], ["5060", 0, 2000]]),  // 返金
  ];
  const r = computeTaxSummary(entries, { taxCategoryByCode: TAX_CAT });
  assert.equal(r.life_insurance.total, 10000);  // 12000 - 2000
});

test("amount == 0 の科目は accounts から除外", () => {
  const entries = [
    entry(1, "journal", [["5050", 5000, 0]]),
    entry(2, "journal", [["5050", 0, 5000]]),  // 完全相殺
    entry(3, "journal", [["5051", 10000, 0]]),
  ];
  const r = computeTaxSummary(entries, {
    taxCategoryByCode: TAX_CAT, accountNameByCode: NAMES,
  });
  // 5050 は除外、5051 のみ残る
  assert.equal(r.social_insurance.accounts.length, 1);
  assert.equal(r.social_insurance.accounts[0].name, "国民年金");
  assert.equal(r.social_insurance.total, 10000);
});

test("tax_category が null/未設定の科目は集計しない", () => {
  const entries = [
    entry(1, "journal", [["5010", 100000, 0]]),  // null
    entry(2, "journal", [["5200", 200000, 0]]),  // null
    entry(3, "journal", [["UNKNOWN", 5000, 0]]),  // マスタにない
    entry(4, "journal", [["5050", 30000, 0]]),
  ];
  const r = computeTaxSummary(entries, { taxCategoryByCode: TAX_CAT });
  assert.equal(Object.keys(r).length, 1);
  assert.equal(r.social_insurance.total, 30000);
});

test("account_name が NAMES にない場合は code をフォールバック", () => {
  const entries = [
    entry(1, "journal", [["5050", 5000, 0]]),
  ];
  const r = computeTaxSummary(entries, { taxCategoryByCode: TAX_CAT });
  assert.equal(r.social_insurance.accounts[0].name, "5050");
});

test("TAX_CATEGORY_LABELS export 確認", () => {
  assert.equal(TAX_CATEGORY_LABELS.social_insurance, "社会保険料控除");
  assert.equal(TAX_CATEGORY_LABELS.life_insurance, "生命保険料控除");
  assert.equal(TAX_CATEGORY_LABELS.donation, "寄附金控除");
  assert.equal(TAX_CATEGORY_LABELS.ideco, "小規模企業共済等掛金控除");
});

test("account_code null の line はスキップ (復号失敗対応)", () => {
  const entries = [
    {id: 1, fiscal_period: 5, source: "journal", lines: [
      {account_code: null, debit: 100, credit: 0},
      {account_code: "5050", debit: 200, credit: 0},
    ]},
  ];
  const r = computeTaxSummary(entries, { taxCategoryByCode: TAX_CAT });
  assert.equal(r.social_insurance.total, 200);
});
