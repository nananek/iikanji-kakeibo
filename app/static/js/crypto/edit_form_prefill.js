// Phase E3-F PR-D-6-3b-3: 編集フォーム (仕訳帳 / 出納帳) の date / description
// prefill をクライアント側 MK 復号で行う。
//
// サーバは平文 date / description を返さなくなった (これらの列は D-6-5 で DROP)。
// 編集画面ロード時に `GET /api/v1/journals/<id>` から encrypted_blob を取得・
// 復号 (fetchEntryFields) し、date input (Alpine fiscalPeriodChecker の dateValue)
// と description input を埋める。
//
// MK ロック中 / 監査代理中は復号できないため prefill をスキップする (編集 submit
// 自体もそれらの状態ではブロックされるため、空欄のままで挙動として一貫する)。

import { SharedCryptoClient } from "./shared-client.js";
import { fetchEntryFields } from "./journals_client.js";


/**
 * 復号済みの entry フィールドを DOM / Alpine に反映する純粋ロジック。
 *
 * date input には Alpine の `x-model="dateValue"` が張られているため、input.value
 * を直接書いても Alpine が上書きしてしまう。よって Alpine スコープの dateValue を
 * 更新し checkDate() を呼ぶ。Alpine 未ロード (テスト) 時は input.value のみ更新。
 *
 * @param {Object} args
 * @param {{date: ?string, description: ?string}} args.fields
 * @param {HTMLInputElement} [args.dateInput]
 * @param {HTMLInputElement|HTMLTextAreaElement} [args.descInput]
 * @param {Object} [args.alpine]  window.Alpine 相当 ($data を持つ)
 */
export function applyEntryPrefill({ fields, dateInput, descInput, alpine }) {
  if (!fields) return;
  if (descInput && typeof fields.description === "string") {
    descInput.value = fields.description;
  }
  if (dateInput && fields.date) {
    dateInput.value = fields.date;
    const scope = alpine && typeof alpine.$data === "function"
      ? alpine.$data(dateInput)
      : null;
    if (scope) {
      scope.dateValue = fields.date;
      if (typeof scope.checkDate === "function") scope.checkDate();
    }
  }
}


/**
 * 編集フォームを hydration する。
 *
 * @param {Object} opts
 * @param {boolean} opts.isEdit
 * @param {number} [opts.entryId]
 * @param {number|bigint} opts.userId
 * @param {boolean} [opts.isProxyMode]
 * @param {HTMLInputElement} [opts.dateInput]
 * @param {HTMLInputElement|HTMLTextAreaElement} [opts.descInput]
 * @param {string} [opts.workerUrl]
 * @param {Object} [opts.alpine]
 * @param {Function} [opts.ClientClass]  テスト DI (default SharedCryptoClient)
 * @param {Function} [opts.fetchFields]  テスト DI (default fetchEntryFields)
 * @param {Function} [opts.fetchImpl]    テスト DI
 * @returns {Promise<?Object>}  反映した fields (skip 時は null)
 */
export async function hydrateEditForm(opts) {
  const {
    isEdit,
    entryId,
    userId,
    isProxyMode = false,
    dateInput,
    descInput,
    workerUrl,
    alpine,
    ClientClass = SharedCryptoClient,
    fetchFields = fetchEntryFields,
    fetchImpl,
  } = opts || {};

  // 新規入力 / 監査代理中は prefill 対象外。
  if (!isEdit || entryId === undefined || entryId === null || isProxyMode) {
    return null;
  }

  const client = new ClientClass(workerUrl);
  try {
    const status = await client.status();
    if (!status || !status.hasKey) {
      // MK ロック中は復号不可。date / description は空欄のまま (submit も
      // ロック中はブロックされる)。
      return null;
    }
    const fields = await fetchFields({ client, userId, entryId, fetchImpl });
    applyEntryPrefill({ fields, dateInput, descInput, alpine });
    return fields;
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}
