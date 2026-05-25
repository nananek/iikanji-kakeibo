// Phase E3-C-8: 医療費控除集計のクライアントサイド集計。
//
// 既に「仕訳 + MedicalExpense マージ済」の expense オブジェクト配列を受けて、
// 受診者別 → 医療機関別の階層集計と合計を返す純粋関数。
//
// 仕訳 + MedicalExpense の fetch / 復号 / マージは呼出側の責務 (本 PR の
// スコープ外)。journals_client.fetchJournalsForYear に加えて、
// medical_expenses_client (E3-C-8b で実装予定) が別途必要。
//
// サーバ側 app/services/tax.get_medical_summary と並存。UI 統合は別 PR
// (E3-C-8c)、サーバ側削除は Phase E7 (E3-F)。


/**
 * 医療費控除集計を計算。
 *
 * @param {Array<Object>} expenses
 *   [{date, description, amount, account_name, patient_name, hospital_name,
 *     treatment_description, provider_type, insurance_reimbursement}]
 *
 * @returns {Object} {
 *   total_paid,
 *   total_reimbursed,
 *   net_total,            // paid - reimbursed
 *   by_patient: [{
 *     name,
 *     paid, reimbursed, net,
 *     hospitals: [{name, paid, reimbursed, net, provider_type}]
 *   }],   // paid 降順
 * }
 *
 * 受診者名・病院名が空文字の場合は "(未設定)" で集約。
 */
export function computeMedicalSummary(expenses) {
  if (!Array.isArray(expenses)) {
    throw new TypeError("expenses must be an array");
  }

  let total_paid = 0;
  let total_reimbursed = 0;

  // patient → {paid, reimbursed, hospitals: Map<name, {paid, reimbursed, provider_type}>}
  const byPatient = new Map();

  for (const e of expenses) {
    const paid = e.amount ?? 0;
    const reimbursed = e.insurance_reimbursement ?? 0;
    total_paid += paid;
    total_reimbursed += reimbursed;

    const patient = e.patient_name || "(未設定)";
    const hospital = e.hospital_name || "(未設定)";

    let p = byPatient.get(patient);
    if (!p) {
      p = { paid: 0, reimbursed: 0, hospitals: new Map() };
      byPatient.set(patient, p);
    }
    p.paid += paid;
    p.reimbursed += reimbursed;

    let h = p.hospitals.get(hospital);
    if (!h) {
      h = { paid: 0, reimbursed: 0, provider_type: e.provider_type || "" };
      p.hospitals.set(hospital, h);
    }
    h.paid += paid;
    h.reimbursed += reimbursed;
  }

  // 受診者別: paid 降順
  const by_patient = [...byPatient.entries()]
    .sort((a, b) => b[1].paid - a[1].paid)
    .map(([name, pdata]) => ({
      name,
      paid: pdata.paid,
      reimbursed: pdata.reimbursed,
      net: pdata.paid - pdata.reimbursed,
      // 病院別: paid 降順
      hospitals: [...pdata.hospitals.entries()]
        .sort((a, b) => b[1].paid - a[1].paid)
        .map(([hname, hdata]) => ({
          name: hname,
          paid: hdata.paid,
          reimbursed: hdata.reimbursed,
          net: hdata.paid - hdata.reimbursed,
          provider_type: hdata.provider_type,
        })),
    }));

  return {
    total_paid,
    total_reimbursed,
    net_total: total_paid - total_reimbursed,
    by_patient,
  };
}
