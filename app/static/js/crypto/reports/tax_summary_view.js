// Phase E3-F-3f: 確定申告控除集計 view 構築の純粋関数。
//
// computeTaxSummary の結果 (tax_category → {label, accounts, total}) を、
// UI 表示順 (社会保険料 → 生命保険料 → ...) の配列に整形する。

const CATEGORY_DISPLAY_ORDER = [
  "social_insurance",
  "life_insurance",
  "earthquake_insurance",
  "donation",
  "ideco",
  "withholding_tax",
  // medical / resident_tax は computeTaxSummary 側で除外済み
];


/**
 * @param {Object} jsResult computeTaxSummary の戻り値
 * @returns {{ sections: Array<{code, label, total, accounts}> }}
 */
export function composeTaxSummaryView(jsResult) {
  if (!jsResult || typeof jsResult !== "object") {
    throw new TypeError("jsResult must be an object");
  }
  const sections = [];
  // 固定順
  for (const cat of CATEGORY_DISPLAY_ORDER) {
    const v = jsResult[cat];
    if (!v) continue;
    sections.push({
      code: cat,
      label: v.label,
      total: v.total,
      accounts: v.accounts.slice(),
    });
  }
  // 既知順序にない category は末尾にコード昇順で追加
  const knownSet = new Set(CATEGORY_DISPLAY_ORDER);
  const extra = Object.keys(jsResult)
    .filter((k) => !knownSet.has(k))
    .sort();
  for (const cat of extra) {
    const v = jsResult[cat];
    sections.push({
      code: cat,
      label: v.label,
      total: v.total,
      accounts: v.accounts.slice(),
    });
  }
  return { sections };
}
