// Phase E3-F-3h: 青色申告決算書 view 構築の純粋関数。
//
// formStructure (TaxFormField + mapping) と 3 種の amounts (P/L 当年発生額、
// BS 期末残高、BS 期首残高) から field_data を組み立てる。サーバ側
// `get_tax_form_report` + `_compute_subtotals` の JS 移植版。
//
// 戻り値:
//   {
//     field_data: [{
//       field: {id, page, section, row_code, name, is_subtotal,
//               is_user_defined, display_order},
//       codes: [account_code, ...],
//       amount: number,
//       opening: number | null,  // P/L 系は null、BS 系は期首残高
//     }],
//   }


/**
 * @param {Object} amountSources
 * @param {Object<string, number>} amountSources.pl_amounts
 *   P/L 当年発生額 (closing 除外、normal_balance 側を正符号)
 * @param {Object<string, number>} amountSources.bs_amounts
 *   B/S 期末残高 (全期間累計, closing 含む)
 * @param {Object<string, number>} amountSources.bs_opening
 *   B/S 期首残高 (対象年度より前の累計, closing 含む)
 * @param {Object} formStructure
 * @param {Array<Object>} formStructure.fields
 *   [{id, page, section, row_code, name, is_subtotal, is_user_defined,
 *     display_order}]
 * @param {Object<number, Array<string>>} formStructure.mappings
 *   {field_id: [account_code, ...]}
 * @returns {{field_data: Array<Object>}}
 */
export function composeTaxFormView(amountSources, formStructure) {
  if (!amountSources || typeof amountSources !== "object") {
    throw new TypeError("amountSources must be an object");
  }
  if (!formStructure || typeof formStructure !== "object") {
    throw new TypeError("formStructure must be an object");
  }
  if (!Array.isArray(formStructure.fields)) {
    throw new TypeError("formStructure.fields must be an array");
  }
  const pl = amountSources.pl_amounts || {};
  const bs = amountSources.bs_amounts || {};
  const bsOpening = amountSources.bs_opening || {};
  const mappings = formStructure.mappings || {};

  // 1. 各 field に codes を結合し amount/opening を初期計算
  const field_data = formStructure.fields.slice().sort(
    (a, b) => (a.display_order || 0) - (b.display_order || 0),
  ).map((f) => {
    const codes = mappings[f.id] || [];
    const item = {
      field: f,
      codes: codes.slice(),
      amount: 0,
      opening: null,
    };
    if (f.is_subtotal) {
      // subtotal は後段の _computeSubtotals で埋める
      if (f.page === 4) item.opening = 0;
      return item;
    }
    if (f.page === 4) {
      // BS: 期首 + 期末
      let opening = 0;
      let closing = 0;
      for (const c of codes) {
        opening += bsOpening[c] || 0;
        closing += bs[c] || 0;
      }
      item.opening = opening;
      item.amount = closing;
    } else {
      // P/L 系
      let amt = 0;
      for (const c of codes) amt += pl[c] || 0;
      item.amount = amt;
    }
    return item;
  });

  // 2. 小計を計算 (サーバ _compute_subtotals の JS 移植)
  _computeSubtotals(field_data);

  return { field_data };
}


function _computeSubtotals(field_data) {
  // section 集計 (is_subtotal=false 行のみ)
  const sectionTotals = new Map();
  for (const item of field_data) {
    const f = item.field;
    if (f.is_subtotal) continue;
    const key = f.page + ":" + f.section;
    let cur = sectionTotals.get(key);
    if (!cur) {
      cur = { amount: 0, opening: 0 };
      sectionTotals.set(key, cur);
    }
    cur.amount += item.amount;
    if (item.opening != null) cur.opening += item.opening;
  }
  const revenue = (sectionTotals.get("1:revenue") || { amount: 0 }).amount;
  const cosTotal = (sectionTotals.get("1:cost_of_sales") || { amount: 0 }).amount;
  const expensesTotal = (sectionTotals.get("1:expenses") || { amount: 0 }).amount;

  function _findByRowCode(rowCode, section) {
    for (const d of field_data) {
      if (d.field.row_code === rowCode && d.field.section === section) {
        return d;
      }
    }
    return null;
  }
  function _findByRowCodeOnly(rowCode) {
    for (const d of field_data) {
      if (d.field.row_code === rowCode) return d;
    }
    return null;
  }

  for (const item of field_data) {
    const f = item.field;
    if (!f.is_subtotal) continue;

    if (f.section === "cost_of_sales" && f.row_code === "4") {
      // 小計 = 期首棚卸 + 仕入
      item.amount = cosTotal;
    } else if (f.section === "cost_of_sales" && f.row_code === "6") {
      // 差引原価 = 小計 - 期末棚卸
      const ending = _findByRowCode("5", "cost_of_sales");
      const endingAmt = ending ? ending.amount : 0;
      item.amount = cosTotal - endingAmt;
    } else if (f.section === "cost_of_sales" && f.row_code === "7") {
      // 差引金額 = 売上 - 差引原価
      const cos = _findByRowCode("6", "cost_of_sales");
      const cosAmt = cos ? cos.amount : 0;
      item.amount = revenue - cosAmt;
    } else if (f.section === "expenses" && f.row_code === "30") {
      item.amount = expensesTotal;
    } else if (f.section === "income" && f.row_code === "31") {
      // 差引金額 = 売上 - 原価 - 経費
      const cos = _findByRowCode("6", "cost_of_sales");
      const cosAmt = cos ? cos.amount : 0;
      item.amount = revenue - cosAmt - expensesTotal;
    } else if (f.section === "income" && f.row_code === "35") {
      // 青色申告特別控除前の所得金額
      const gross = _findByRowCode("31", "income");
      const grossAmt = gross ? gross.amount : 0;
      const r32 = _findByRowCodeOnly("32");
      const r33 = _findByRowCodeOnly("33");
      const r34 = _findByRowCodeOnly("34");
      const senshusha = r32 ? r32.amount : 0;
      const kurimodoshi = r33 ? r33.amount : 0;
      const kuriire = r34 ? r34.amount : 0;
      item.amount = grossAmt - senshusha + kurimodoshi - kuriire;
    } else if (f.section === "income" && f.row_code === "37") {
      // 所得金額 = 控除前 - 控除額
      const r35 = _findByRowCodeOnly("35");
      const r36 = _findByRowCodeOnly("36");
      const before = r35 ? r35.amount : 0;
      const deduction = r36 ? r36.amount : 0;
      item.amount = before - deduction;
    } else if (f.section === "bs_assets" && f.row_code === "AT") {
      const t = sectionTotals.get("4:bs_assets") || { amount: 0, opening: 0 };
      item.amount = t.amount;
      item.opening = t.opening;
    } else if (f.section === "bs_liabilities" && f.row_code === "LT") {
      const t = sectionTotals.get("4:bs_liabilities") || { amount: 0, opening: 0 };
      item.amount = t.amount;
      item.opening = t.opening;
    }
  }
}
