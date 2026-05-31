// Phase E3-C-8b: クライアント側 MedicalExpense 取得 + 復号 helper。
//
// GET /api/v1/medical-expenses?fiscal_year=Y を取得し、encrypted_blob / blob_iv
// を MK で復号して plain object 配列を返す。
//
// medical_summary.js (E3-C-8) の入力として、journals_client と組み合わせて
// 医療費控除画面で使う。

import { b64decode } from "./b64.js";
import { buildAAD, decryptRecord } from "./record.js";


async function _normalize(client, userId, apiExpense) {
  let body = null;
  if (apiExpense.encrypted_blob && apiExpense.blob_iv) {
    try {
      const blob = b64decode(apiExpense.encrypted_blob);
      const iv = b64decode(apiExpense.blob_iv);
      // E3-F PR-A: AAD は Option B (user_id のみ)。
      const aad = buildAAD("me", userId);
      body = await decryptRecord(client, blob, iv, aad);
    } catch (e) {
      // E3-F PR-D-6-5-pre1: 復号失敗時はフィールドを空にする (サーバは平文を
      // 返さなくなったため平文フォールバックは無い)。
      console.warn(
        `medical_expenses_client: expense ${apiExpense.id} decrypt failed: ` +
        `${e?.message || e}`,
      );
    }
  }
  return {
    id: apiExpense.id,
    journal_entry_id: apiExpense.journal_entry_id,
    // E3-F PR-D-6-5-pre1: 平文列は復号 body からのみ取得 (サーバ平文は撤去)。
    date: body?.date ?? null,
    patient_name: body?.patient_name ?? "",
    hospital_name: body?.hospital_name ?? "",
    treatment_description: body?.treatment_description ?? "",
    provider_type: body?.provider_type ?? "",
    amount_paid: body?.amount_paid ?? 0,
    insurance_reimbursement: body?.insurance_reimbursement ?? 0,
  };
}


/**
 * 指定年度の MedicalExpense をすべて取得 + 復号。
 *
 * @param {Object} args
 * @param {Object} args.client     SharedCryptoClient
 * @param {number|bigint} args.userId
 * @param {number} args.fiscalYear
 * @param {Function} [args.fetchImpl]
 * @returns {Promise<Array<Object>>}
 *   medical_summary.computeMedicalSummary に渡せる形式
 */
export async function fetchMedicalExpensesForYear({
  client, userId, fiscalYear, fetchImpl,
}) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  if (userId === undefined || userId === null) {
    throw new Error("userId is required");
  }
  if (typeof userId !== "number" && typeof userId !== "bigint") {
    throw new Error("userId must be a number or bigint");
  }
  if (typeof userId === "number" && !Number.isSafeInteger(userId)) {
    throw new Error("userId Number must be a safe integer (use BigInt for > 2^53)");
  }
  if (!Number.isInteger(fiscalYear) || !(1900 <= fiscalYear && fiscalYear <= 2200)) {
    throw new Error("fiscalYear must be int in 1900..2200");
  }
  const f = fetchImpl ?? globalThis.fetch;

  const url = `/api/v1/medical-expenses?fiscal_year=${fiscalYear}`;
  const r = await f(url, { credentials: "include" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(
      `fetchMedicalExpensesForYear: HTTP ${r.status} ${e.error || ""}`,
    );
  }
  const body = await r.json();
  const list = body.expenses || [];
  // 復号は逐次 (件数が少ないので並列化のメリット小、メモリ peak 削減)
  // ただし journals_client と同様に Promise.all で並列化しても良い。
  const result = [];
  for (const item of list) {
    result.push(await _normalize(client, userId, item));
  }
  return result;
}
