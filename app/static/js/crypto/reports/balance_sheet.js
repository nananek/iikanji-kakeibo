// Phase E3-C-4: 貸借対照表 (Balance Sheet) のクライアントサイド集計。
//
// journals_client.fetchJournalsForYear の戻り値から、資産・負債・純資産
// の残高を集計し当期純利益を加算した B/S を返す純粋関数。
//
// サーバ側 app/views/reports.bs と並存。UI 統合は別 PR (E3-C-4b)、
// サーバ側削除は Phase E7 (E3-F)。
//
// 注意: B/S は「指定年度末時点の累計」なので、entries には対象年度までの
// **全 entry** を渡す必要がある (journals_client を複数年度に対して呼んで
// 合算する責務は呼出側)。本関数は与えられた entries の合算のみ行う。
//
// 設計書 §12.3 参照。

import { computeProfitLoss } from "./profit_loss.js";


/**
 * 貸借対照表を計算。
 *
 * @param {Array<Object>} entries
 *   journals_client の戻り値形式。複数年度分を含めて OK (本関数は
 *   fiscal_year でフィルタしない)。
 * @param {Object} options
 * @param {Object} options.accountTypeByCode
 *   {[code]: "asset"|"liability"|"equity"|"revenue"|"expense"}
 * @param {Object} options.normalBalanceByCode
 *   {[code]: "debit"|"credit"} — 資産=debit, 負債/純資産/収益=credit, 費用=debit
 * @param {Object} [options.accountNameByCode]
 *   表示名マッピング
 *
 * @returns {Object} {
 *   assets: [{account_code, account_name, balance}],
 *   liabilities: [{account_code, account_name, balance}],
 *   equities: [{account_code, account_name, balance}],
 *   total_assets, total_liabilities, total_equity,
 *   net_income,    // P/L 当期純利益
 *   has_closing,   // 当該 entries に source='closing' があるか
 *   total_liability_and_equity,  // 負債+純資産+当期純利益 (損益振替前のみ加算)
 * }
 *
 * 計算ルール:
 * - asset (normal=debit): balance = debit - credit
 * - liability/equity (normal=credit): balance = credit - debit
 * - balance == 0 の科目は breakdown から除外
 * - 損益振替前 (has_closing=false): 当期純利益を total_equity に加算
 *   (繰越利益に未振替なので、B/S 負債+純資産側に「当期純利益」として表示)
 * - 損益振替後 (has_closing=true): 当期純利益は繰越利益に含まれるので加算なし
 */
export function computeBalanceSheet(entries, options) {
  if (!Array.isArray(entries)) {
    throw new TypeError("entries must be an array");
  }
  if (!options || !options.accountTypeByCode) {
    throw new TypeError("options.accountTypeByCode is required");
  }
  if (!options.normalBalanceByCode) {
    throw new TypeError("options.normalBalanceByCode is required");
  }
  const {
    accountTypeByCode,
    normalBalanceByCode,
    accountNameByCode = {},
  } = options;

  // BS 科目別の debit/credit 累計 (closing 仕訳も含む = include_closing=true 相当)
  const sums = new Map();  // code → {debit, credit, type}
  let hasClosing = false;

  for (const entry of entries) {
    if (entry.source === "closing") hasClosing = true;
    for (const line of entry.lines || []) {
      const code = line.account_code;
      if (code == null) continue;
      const type = accountTypeByCode[code];
      // BS 科目のみ集計
      if (type !== "asset" && type !== "liability" && type !== "equity") {
        continue;
      }
      const cur = sums.get(code) ?? { debit: 0, credit: 0, type };
      cur.debit += line.debit ?? 0;
      cur.credit += line.credit ?? 0;
      sums.set(code, cur);
    }
  }

  const assets = [];
  const liabilities = [];
  const equities = [];
  for (const [code, { debit, credit, type }] of sums.entries()) {
    const normal = normalBalanceByCode[code];
    if (normal !== "debit" && normal !== "credit") continue;
    const balance = normal === "debit" ? debit - credit : credit - debit;
    if (balance === 0) continue;
    const row = {
      account_code: code,
      account_name: accountNameByCode[code] ?? code,
      balance,
    };
    if (type === "asset") assets.push(row);
    else if (type === "liability") liabilities.push(row);
    else if (type === "equity") equities.push(row);
  }
  assets.sort((a, b) => a.account_code.localeCompare(b.account_code));
  liabilities.sort((a, b) => a.account_code.localeCompare(b.account_code));
  equities.sort((a, b) => a.account_code.localeCompare(b.account_code));

  const total_assets = assets.reduce((s, r) => s + r.balance, 0);
  const total_liabilities = liabilities.reduce((s, r) => s + r.balance, 0);
  const total_equity = equities.reduce((s, r) => s + r.balance, 0);

  // 当期純利益 (損益振替前なら P/L から、振替後なら 0 = 既に繰越利益に含む)
  let net_income = 0;
  if (!hasClosing) {
    const pl = computeProfitLoss(entries, {
      accountTypeByCode,
      accountNameByCode,
      // 月指定なし = 年間 (期首/通常月/決算整理) + closing 除外 (entries に
      // closing がある = hasClosing なので、この分岐自体に来ない)
    });
    net_income = pl.net_income;
  }

  return {
    assets,
    liabilities,
    equities,
    total_assets,
    total_liabilities,
    total_equity,
    net_income,
    has_closing: hasClosing,
    total_liability_and_equity:
      total_liabilities + total_equity + (hasClosing ? 0 : net_income),
  };
}
