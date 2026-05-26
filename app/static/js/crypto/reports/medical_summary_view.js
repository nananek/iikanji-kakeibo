// Phase E3-F-3g: 医療費控除 view 構築の純粋関数。
//
// computeMedicalSummary の結果 (totals + by_patient) と
// 詳細用 expenses 配列を結合し、UI 表示用の view を返す。
//
// 戻り値:
//   {
//     totals: { paid, reimbursed, net },
//     by_patient: [...],
//     expenses_list: [{
//       date, description, patient_name, hospital_name,
//       provider_type, amount, insurance_reimbursement, account_name,
//     }],   // date 昇順
//   }


/**
 * @param {Object} computeResult computeMedicalSummary の戻り値
 * @param {Array<Object>} expenses マージ済 expense 配列
 *   ({date, description, amount, account_name, patient_name,
 *     hospital_name, treatment_description, provider_type,
 *     insurance_reimbursement})
 * @returns {Object}
 */
export function composeMedicalSummaryView(computeResult, expenses = []) {
  if (!computeResult || typeof computeResult !== "object") {
    throw new TypeError("computeResult must be an object");
  }
  if (!Array.isArray(expenses)) {
    throw new TypeError("expenses must be an array");
  }

  const list = expenses.slice().sort((a, b) => {
    // date は ISO 文字列 (or null) — string compare で昇順
    const ad = a.date || "";
    const bd = b.date || "";
    if (ad === bd) return 0;
    return ad < bd ? -1 : 1;
  });

  return {
    totals: {
      paid: computeResult.total_paid || 0,
      reimbursed: computeResult.total_reimbursed || 0,
      net: computeResult.net_total || 0,
    },
    by_patient: (computeResult.by_patient || []).slice(),
    expenses_list: list,
  };
}
