// Phase E3-F PR-D-3: クライアントサイド MedicalExpense 起票 helper。
//
// サーバ側 medical.new / medical.api_update に相当する「医療費明細を
// 暗号化して POST する」ロジックを JS 純粋関数として実装する。医療費 UI が
// サーバレンダ → client 描画 + client 暗号化に移行する際に使う。
//
// 戻り値: POST /api/v1/medical-expenses にそのまま送れる形
// (E3-F PR-D-6-6: wire 平文除去後):
//   {
//     journal_entry_id,
//     encrypted_blob, blob_iv,
//   }
//
// 平文 date / patient_name / hospital_name / treatment_description /
// provider_type / amount_paid / insurance_reimbursement は wire に乗せない
// (本体は encrypted_blob に格納済、列は 055 で DROP 済)。サーバが平文で必要と
// するメタは journal_entry_id のみ。
//
// client + userId を渡すと body 全体を MK で AES-GCM 暗号化し
// {journal_entry_id, encrypted_blob, blob_iv} を返す。client なしでは暗号化前の
// 論理レコード (全フィールド) を同期返却する (テスト用途)。
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
 *   - client 指定時: Promise<{journal_entry_id, encrypted_blob, blob_iv}>
 *   - client 未指定時: 暗号化前の論理レコード (全フィールド・同期)
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

  // 暗号化前の論理レコード (medical_expenses_client._normalize が復号で期待する
  // shape と同じ)。client なしのときはこれをそのまま返す (テスト用途)。
  const record = {
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
    return record;
  }

  _validateUserId(userId);

  const body = {
    v: 1,
    date: record.date,
    patient_name: record.patient_name,
    hospital_name: record.hospital_name,
    treatment_description: record.treatment_description,
    provider_type: record.provider_type,
    amount_paid: record.amount_paid,
    insurance_reimbursement: record.insurance_reimbursement,
  };
  const aad = buildAAD("me", userId);
  // E3-F PR-D-6-6: wire に乗せる平文は journal_entry_id のみ。明細の実値は
  // encrypted_blob に格納する。
  return encryptRecord(client, body, aad).then((enc) => ({
    journal_entry_id: journalEntryId,
    encrypted_blob: b64encode(enc.blob),
    blob_iv: b64encode(enc.iv),
  }));
}
