// #338 PR3 (方針B): 仕訳編集フォーム (journal/form.html) の明細行
// (account_code / debit_amount / credit_amount / description) prefill を
// クライアント側 MK 復号で行う。
//
// サーバは平文の金額・科目コードを返さなくなった (line id のみのプレースホルダ行を
// 返す)。編集画面ロード時に `GET /api/v1/journals/<id>` から各 line の encrypted_blob
// を取得・復号 (fetchEntryForDiff) し、journalLines Alpine スコープの各行 (line id で
// 照合) に科目コード・金額・摘要を埋める。
//
// closing (損益振替) 仕訳は check_entry_modifiable が編集を弾くため、ここに到達する
// 仕訳の明細は必ず encrypted_blob を持つ。MK ロック中 / 監査代理中は復号できないため
// prefill をスキップする (編集 submit 自体もそれらの状態ではブロックされるため、行は
// 空欄のまま挙動として一貫する。date / description hydration と同じ方針)。

import { SharedCryptoClient } from "./shared-client.js";
import { fetchEntryForDiff } from "./journals_client.js";


/**
 * 復号済みの仕訳明細を journalLines スコープの各行へ反映する純粋関数。
 *
 * journalLines.init は config.lines (= サーバの existing_lines、line id のみ) から
 * 行を生成済み。本関数はその各行 (line.id) を復号済み entryLines (同じ line id) と
 * 照合し、account_code / account_name / debit_amount / credit_amount / description を
 * 設定する。id を持たない行 (新規追加行) は対象外。
 *
 * @param {Object} args
 * @param {Array<{id, account_code, debit, credit, description}>} args.entryLines
 *   復号済み明細 (fetchEntryForDiff の lines)。
 * @param {{lines: Array<Object>}} args.linesScope  journalLines Alpine データ。
 * @param {Function} [args.nameResolver]  (code) => 科目名。省略時は account_name を
 *   既存値のままにする (account_code のみ更新)。
 */
export function applyJournalLines({ entryLines, linesScope, nameResolver }) {
  if (!linesScope || !Array.isArray(linesScope.lines)) return;
  if (!Array.isArray(entryLines)) return;

  const byId = new Map();
  for (const l of entryLines) {
    if (l && l.id != null) byId.set(l.id, l);
  }
  for (const line of linesScope.lines) {
    if (!line || line.id == null || !byId.has(line.id)) continue;
    const src = byId.get(line.id);
    line.account_code = src.account_code || "";
    if (typeof nameResolver === "function" && line.account_code) {
      line.account_name = nameResolver(line.account_code) || "";
    }
    line.debit_amount = src.debit || 0;
    line.credit_amount = src.credit || 0;
    line.description = src.description || "";
  }
}


/**
 * 仕訳編集フォームの明細行を hydration する。
 *
 * @param {Object} opts
 * @param {boolean} opts.isEdit
 * @param {number} [opts.entryId]
 * @param {number|bigint} opts.userId
 * @param {HTMLElement} [opts.formEl]  journalLines x-data を張った form 要素
 * @param {Function} [opts.nameResolver]  科目コード → 科目名 (window._acctNameByCode)
 * @param {string} [opts.workerUrl]
 * @param {Object} [opts.alpine]
 * @param {Function} [opts.ClientClass]  テスト DI (default SharedCryptoClient)
 * @param {Function} [opts.fetchEntry]   テスト DI (default fetchEntryForDiff)
 * @param {Function} [opts.fetchImpl]    テスト DI
 * @returns {Promise<?Array>}  反映した entryLines (skip 時は null)
 */
export async function hydrateJournalLines(opts) {
  const {
    isEdit,
    entryId,
    userId,
    formEl,
    nameResolver,
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
      // MK ロック中は復号不可。明細行は空欄のまま (submit もロック中はブロック)。
      return null;
    }
    const entry = await fetchEntry({ client, userId, entryId, fetchImpl });
    const entryLines = entry ? entry.lines : null;
    const linesScope = alpine && formEl && typeof alpine.$data === "function"
      ? alpine.$data(formEl)
      : null;
    applyJournalLines({ entryLines, linesScope, nameResolver });
    return entryLines || null;
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}
