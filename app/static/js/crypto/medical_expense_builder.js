// Phase E3-F PR-D-3: クライアントサイド MedicalExpense 起票 helper。
//
// サーバ側 medical.new / medical.api_update に相当する「医療費明細を
// 暗号化して POST する」ロジックを JS 純粋関数として実装する。医療費 UI が
// サーバレンダ → client 描画 + client 暗号化に移行する際に使う。
//
// 戻り値: POST /api/v1/medical-expenses にそのまま送れる形:
//   {
//     journal_entry_id,
//     date, patient_name, hospital_name, treatment_description,
//     provider_type, amount_paid, insurance_reimbursement,
//     encrypted_blob, blob_iv,
//   }
//
// dual-storage 期間中は旧平文フィールドも併送する (サーバ側が両方を保存し、
// バックアップ等の既存平文リーダ互換を保つ)。平文 WRITE 停止は後続 PR。
//
// client + userId を渡すと body 全体を MK で AES-GCM 暗号化し encrypted_blob /
// blob_iv を付与する。client なしでは平文 payload を返す (テスト用途)。
// AAD は Option B (buildAAD("me", userId)、medical_expenses は user_id のみで
// 一意。entry_id 等は含めない — record.js / §12.2 参照)。

import { buildAAD, encryptRecord } from "./record.js";
import { b64encode } from "./b64.js";


function _validateUserId(userId) {
  if (typeof userId !== "number" && typeof userId !== "bigint") {
    throw new TypeError("userId must be a number or bigint");
  }
  if (typeof userId === "number" && !Number.isSafeInteger(userId)) {
    throw new TypeError(
      "userId Number must be a safe integer (use BigInt for > 2^53)",
    );
  }
}


function _assertIntAmount(amount, label) {
  // 金額は 0 以上の整数。負数や小数は黙って丸めず fail-loud。
  if (!Number.isInteger(amount) || amount < 0) {
    throw new TypeError(`${label} must be a non-negative integer (got ${amount})`);
  }
}


/**
 * 医療費明細の入力から POST /api/v1/medical-expenses 用 payload を生成する。
 *
 * client + userId が指定された場合は body を MK で暗号化して encrypted_blob /
 * blob_iv を付与する。client なしでも平文 payload を返す (テスト用途)。
 *
 * @param {Object} opts
 * @param {Object} [opts.client]              SharedCryptoClient (暗号化する場合)
 * @param {number|bigint} [opts.userId]       (client 指定時に必須)
 * @param {number|bigint} opts.journalEntryId 紐付ける仕訳 ID
 * @param {string|null} [opts.date]           ISO 形式 (YYYY-MM-DD) or null
 * @param {string} [opts.patientName=""]
 * @param {string} [opts.hospitalName=""]
 * @param {string} [opts.treatmentDescription=""]
 * @param {string|null} [opts.providerType=null]  hospital/pharmacy/nursing/other
 * @param {number} [opts.amountPaid=0]        支払額 (非負整数)
 * @param {number} [opts.insuranceReimbursement=0]  保険補填額 (非負整数)
 * @returns {Promise<Object>|Object} medical-expenses POST 用 payload
 *   - client 指定時: Promise<encrypted payload>
 *   - client 未指定時: 平文 payload (同期)
 */
export function buildMedicalExpense({
  client,
  userId,
  journalEntryId,
  date = null,
  patientName = "",
  hospitalName = "",
  treatmentDescription = "",
  providerType = null,
  amountPaid = 0,
  insuranceReimbursement = 0,
}) {
  if (journalEntryId === undefined || journalEntryId === null) {
    throw new TypeError("journalEntryId is required");
  }
  _assertIntAmount(amountPaid, "amountPaid");
  _assertIntAmount(insuranceReimbursement, "insuranceReimbursement");

  // provider_type は空文字を null に正規化 (DB は nullable)。
  const provider = providerType ? providerType : null;

  const payload = {
    journal_entry_id: journalEntryId,
    date: date || null,
    patient_name: patientName || "",
    hospital_name: hospitalName || "",
    treatment_description: treatmentDescription || "",
    provider_type: provider,
    amount_paid: amountPaid,
    insurance_reimbursement: insuranceReimbursement,
  };

  if (!client) {
    return payload;
  }

  _validateUserId(userId);

  // 暗号化 body は medical_expenses_client._normalize が復号で期待する shape。
  const body = {
    v: 1,
    date: payload.date,
    patient_name: payload.patient_name,
    hospital_name: payload.hospital_name,
    treatment_description: payload.treatment_description,
    provider_type: payload.provider_type,
    amount_paid: payload.amount_paid,
    insurance_reimbursement: payload.insurance_reimbursement,
  };
  const aad = buildAAD("me", userId);
  return encryptRecord(client, body, aad).then((enc) => ({
    ...payload,
    encrypted_blob: b64encode(enc.blob),
    blob_iv: b64encode(enc.iv),
  }));
}
