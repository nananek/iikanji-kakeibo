// Phase E3-C-6: 確定申告控除集計 (Tax Summary) のクライアントサイド集計。
//
// tax_category がついた科目の年間 debit-credit を、控除種別ごとに集計する
// 純粋関数。medical (医療費控除) と resident_tax (住民税) は別画面で
// 専用集計するため、本関数では除外。
//
// サーバ側 app/services/tax.get_tax_summary と並存。UI 統合は別 PR
// (E3-C-6b)、サーバ側削除は Phase E7 (E3-F)。


// サーバ側 TAX_CATEGORY_LABELS と同一の表示ラベル。account マスタ API が
// label を返さないケースに備えて helper として export。
export const TAX_CATEGORY_LABELS = {
  social_insurance: "社会保険料控除",
  life_insurance: "生命保険料控除",
  earthquake_insurance: "地震保険料控除",
  medical: "医療費控除",
  donation: "寄附金控除",
  ideco: "小規模企業共済等掛金控除",
  withholding_tax: "源泉所得税",
  resident_tax: "住民税",
};


// medical / resident_tax はそれぞれ専用集計画面があるため、本関数では除外
const EXCLUDED_TAX_CATEGORIES = new Set(["medical", "resident_tax"]);


/**
 * 確定申告控除集計を計算。
 *
 * @param {Array<Object>} entries
 *   journals_client の戻り値形式 (1 年分の正規化 entry 配列)
 * @param {Object} options
 * @param {Object} options.taxCategoryByCode
 *   {[account_code]: "social_insurance"|"life_insurance"|... | null}
 *   account マスタから取得して呼出側が渡す。null は集計対象外 (tax_category
 *   未設定)。
 * @param {Object} [options.accountNameByCode]  表示名マッピング
 *
 * @returns {Object<string, {label, accounts: [{name, amount}], total}>}
 *   tax_category をキーとする dict (medical/resident_tax は除外)。
 *   accounts は name 昇順、total >= 0 のもののみ。
 *
 * 集計式: amount = debit - credit (借方発生額 = 支出額)
 *   - source="closing" 仕訳は除外
 *   - tax_category が medical / resident_tax の科目は除外
 *   - amount == 0 の科目は accounts から除外
 *   - total == 0 の category は結果に含めない
 */
export function computeTaxSummary(entries, options) {
  if (!Array.isArray(entries)) {
    throw new TypeError("entries must be an array");
  }
  if (!options || !options.taxCategoryByCode) {
    throw new TypeError("options.taxCategoryByCode is required");
  }
  const {
    taxCategoryByCode,
    accountNameByCode = {},
  } = options;

  // tax_category → { code → {debit, credit} }
  const byCategory = new Map();

  for (const entry of entries) {
    if (entry.source === "closing") continue;
    for (const line of entry.lines || []) {
      const code = line.account_code;
      if (code == null) continue;
      const cat = taxCategoryByCode[code];
      if (cat == null) continue;
      if (EXCLUDED_TAX_CATEGORIES.has(cat)) continue;

      let bucket = byCategory.get(cat);
      if (!bucket) {
        bucket = new Map();
        byCategory.set(cat, bucket);
      }
      const cur = bucket.get(code) ?? { debit: 0, credit: 0 };
      cur.debit += line.debit ?? 0;
      cur.credit += line.credit ?? 0;
      bucket.set(code, cur);
    }
  }

  const result = {};
  for (const [cat, bucket] of byCategory.entries()) {
    const accounts = [];
    let total = 0;
    for (const [code, { debit, credit }] of bucket.entries()) {
      const amount = debit - credit;
      if (amount === 0) continue;
      accounts.push({
        name: accountNameByCode[code] ?? code,
        amount,
      });
      total += amount;
    }
    if (total === 0 && accounts.length === 0) continue;
    accounts.sort((a, b) => a.name.localeCompare(b.name));
    result[cat] = {
      label: TAX_CATEGORY_LABELS[cat] ?? cat,
      accounts,
      total,
    };
  }
  return result;
}
