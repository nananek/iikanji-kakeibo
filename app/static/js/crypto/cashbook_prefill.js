// #338 PR2 (方針B): 出納帳編集フォームの「取引種類・支払/科目・金額」prefill を
// クライアント側 MK 復号で行う。
//
// サーバは平文の金額・科目コード (debit_amount / credit_amount / account_code) を
// 読む「3 方向検出」を撤去した (#338 で平文列を DROP するための読み移行)。編集画面
// ロード時に `GET /api/v1/journals/<id>` から encrypted_blob を取得・復号
// (fetchEntryForDiff) し、旧サーバ実装と等価なロジックで取引種類タブ・支払元/科目・
// 金額を埋める。
//
// MK ロック中 / 監査代理中は復号できないため prefill をスキップする (編集 submit
// 自体もそれらの状態ではブロックされるため、空欄のままで挙動として一貫する。
// date / description の hydration (edit_form_prefill.js) と同じ方針)。

import { SharedCryptoClient } from "./shared-client.js";
import { fetchEntryForDiff } from "./journals_client.js";

// 出納帳の取引種類判定における貸借対照表科目 (旧サーバ実装の
// bs_types = {"asset", "liability"} と一致させること。equity は含めない)。
const BS_TYPES = new Set(["asset", "liability"]);


/**
 * 復号済みの仕訳明細から出納帳フォームのフィールドを導出する純粋関数。
 *
 * 旧サーバ実装 (cashbook.edit の 3 方向検出) と等価:
 *   - debit_is_bs && credit_is_bs → transfer (payment=credit / category=debit)
 *   - debit_is_bs                 → income   (payment=debit  / category=credit)
 *   - それ以外                    → expense  (payment=credit / category=debit)
 * 金額はいずれも借方明細の金額。明細が 2 行でない / 借方・貸方明細が揃わない
 * 場合は null を返す (フォームは既定値のまま)。
 *
 * @param {Object} args
 * @param {Array<{account_code: ?string, debit: ?number, credit: ?number}>} args.lines
 * @param {Object<string, {name?: string, type_code?: string}>} args.acctMetaByCode
 *   科目コード → {name, type_code} メタ (無効化済みを含む全科目)。
 * @returns {?{transactionType: string, paymentCode: string, paymentName: string,
 *   categoryCode: string, categoryName: string, amount: number}}
 */
export function detectCashbookFields({ lines, acctMetaByCode }) {
  if (!Array.isArray(lines) || lines.length !== 2) return null;
  const debitLine = lines.find((l) => l && (l.debit || 0) > 0);
  const creditLine = lines.find((l) => l && (l.credit || 0) > 0);
  if (!debitLine || !creditLine) return null;

  const meta = acctMetaByCode || {};
  const isBs = (code) => {
    const m = meta[code];
    return !!(m && BS_TYPES.has(m.type_code));
  };
  const nameOf = (code) => {
    const m = meta[code];
    return (m && m.name) || "";
  };

  const debitIsBs = isBs(debitLine.account_code);
  const creditIsBs = isBs(creditLine.account_code);
  const amount = Math.trunc(debitLine.debit || 0);

  if (debitIsBs && creditIsBs) {
    return {
      transactionType: "transfer",
      paymentCode: creditLine.account_code,
      paymentName: nameOf(creditLine.account_code),
      categoryCode: debitLine.account_code,
      categoryName: nameOf(debitLine.account_code),
      amount,
    };
  }
  if (debitIsBs) {
    return {
      transactionType: "income",
      paymentCode: debitLine.account_code,
      paymentName: nameOf(debitLine.account_code),
      categoryCode: creditLine.account_code,
      categoryName: nameOf(creditLine.account_code),
      amount,
    };
  }
  return {
    transactionType: "expense",
    paymentCode: creditLine.account_code,
    paymentName: nameOf(creditLine.account_code),
    categoryCode: debitLine.account_code,
    categoryName: nameOf(debitLine.account_code),
    amount,
  };
}


/**
 * 導出済みフィールドを Alpine フォームスコープ / amount input に反映する。
 *
 * tab / paymentCode / paymentName / categoryCode / categoryName は form 要素に
 * 張られた Alpine x-data に直接書き込む (switchTab を経由すると categoryCode が
 * リセットされるため使わない)。amount は x-model 非バインドの素の input なので
 * value を直接設定する (submit 時に querySelector で読まれる)。
 *
 * @param {Object} args
 * @param {?Object} args.fields  detectCashbookFields の戻り値
 * @param {HTMLElement} [args.formEl]  Alpine x-data を張った form 要素
 * @param {HTMLInputElement} [args.amountInput]
 * @param {Object} [args.alpine]  window.Alpine 相当 ($data を持つ)
 */
export function applyCashbookFields({ fields, formEl, amountInput, alpine }) {
  if (!fields) return;
  if (amountInput) {
    amountInput.value = String(fields.amount);
  }
  const scope = alpine && formEl && typeof alpine.$data === "function"
    ? alpine.$data(formEl)
    : null;
  if (scope) {
    scope.tab = fields.transactionType;
    scope.paymentCode = fields.paymentCode;
    scope.paymentName = fields.paymentName;
    scope.categoryCode = fields.categoryCode;
    scope.categoryName = fields.categoryName;
  }
}


/**
 * 出納帳編集フォームを hydration する。
 *
 * @param {Object} opts
 * @param {boolean} opts.isEdit
 * @param {number} [opts.entryId]
 * @param {number|bigint} opts.userId
 * @param {Object<string, {name?: string, type_code?: string}>} opts.acctMetaByCode
 * @param {HTMLElement} [opts.formEl]
 * @param {HTMLInputElement} [opts.amountInput]
 * @param {string} [opts.workerUrl]
 * @param {Object} [opts.alpine]
 * @param {Function} [opts.ClientClass]  テスト DI (default SharedCryptoClient)
 * @param {Function} [opts.fetchEntry]   テスト DI (default fetchEntryForDiff)
 * @param {Function} [opts.fetchImpl]    テスト DI
 * @returns {Promise<?Object>}  反映した fields (skip 時は null)
 */
export async function hydrateCashbookEdit(opts) {
  const {
    isEdit,
    entryId,
    userId,
    acctMetaByCode,
    formEl,
    amountInput,
    workerUrl,
    alpine,
    ClientClass = SharedCryptoClient,
    fetchEntry = fetchEntryForDiff,
    fetchImpl,
  } = opts || {};

  // 新規入力は prefill 対象外。
  if (!isEdit || entryId === undefined || entryId === null) {
    return null;
  }

  const client = new ClientClass(workerUrl);
  try {
    const status = await client.status();
    if (!status || !status.hasKey) {
      // MK ロック中は復号不可。取引種類/科目/金額は既定値のまま (submit も
      // ロック中はブロックされる)。
      return null;
    }
    const entry = await fetchEntry({ client, userId, entryId, fetchImpl });
    const fields = detectCashbookFields({
      lines: entry ? entry.lines : null,
      acctMetaByCode,
    });
    applyCashbookFields({ fields, formEl, amountInput, alpine });
    return fields;
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}
