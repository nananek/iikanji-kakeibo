// Phase E3-C-3: 損益計算書 (P/L) のクライアントサイド集計。
//
// journals_client.fetchJournalsForYear の正規化 entry 配列から、収益・費用
// 科目を集計し損益を計算する純粋関数。
//
// サーバ側 app/services/reports.compute_income_statement と並存。UI 統合は
// 別 PR (E3-C-3b)、サーバ側削除は Phase E7 (E3-F)。
//
// 設計書 §12.3 参照。


/**
 * 損益計算書を計算。
 *
 * @param {Array<Object>} entries
 *   journals_client.fetchJournalsForYear の戻り値形式
 *   [{fiscal_period, source, lines: [{account_code, debit, credit}]}]
 * @param {Object} options
 * @param {Object} options.accountTypeByCode
 *   {[account_code]: "revenue"|"expense"|"asset"|...} のマッピング。
 *   呼出側で /accounts API から取得して渡す (本関数は account マスタを
 *   fetch しない)。
 * @param {Object} [options.accountNameByCode]
 *   {[account_code]: 表示名} 任意。breakdown の account_name に使う。
 * @param {number} [options.month]
 *   1..12 を指定すると当該月 (fiscal_period == month) のみ集計。
 *   未指定なら期首 (fp=0) と通常月 1..12 と決算整理 13..15 を含み、
 *   損益振替 (fp=16) と closing 仕訳は除外 (= 年間 P/L)。
 *
 * @returns {Object} {
 *   income_total, expense_total, net_income,
 *   income_breakdown: [{account_code, account_name, amount}],   // amount>0
 *   expense_breakdown: [{account_code, account_name, amount}],  // amount>0
 * }
 *
 * 振り分け規則:
 * - revenue 科目 → credit - debit (貸方残) を amount に
 * - expense 科目 → debit - credit (借方残) を amount に
 * - amount == 0 の科目は breakdown から除外
 */
export function computeProfitLoss(entries, options) {
  if (!Array.isArray(entries)) {
    throw new TypeError("entries must be an array");
  }
  if (!options || !options.accountTypeByCode) {
    throw new TypeError("options.accountTypeByCode is required");
  }
  const {
    accountTypeByCode,
    accountNameByCode = {},
    month,
  } = options;

  // accountCode → { debit, credit, type }
  const sums = new Map();

  for (const entry of entries) {
    const fp = entry.fiscal_period ?? 0;
    // closing 仕訳は除外
    if (entry.source === "closing") continue;
    // 月指定なら当該月のみ。それ以外は fp=16 (損益振替) を除外
    if (month != null) {
      if (fp !== month) continue;
    } else {
      if (fp === 16) continue;
    }

    for (const line of entry.lines || []) {
      const code = line.account_code;
      if (code == null) continue;
      const type = accountTypeByCode[code];
      if (type !== "revenue" && type !== "expense") continue;
      const cur = sums.get(code) ?? { debit: 0, credit: 0, type };
      cur.debit += line.debit ?? 0;
      cur.credit += line.credit ?? 0;
      sums.set(code, cur);
    }
  }

  const incomeBreakdown = [];
  const expenseBreakdown = [];
  for (const [code, { debit, credit, type }] of sums.entries()) {
    const amount = type === "revenue" ? credit - debit : debit - credit;
    if (amount === 0) continue;
    const row = {
      account_code: code,
      account_name: accountNameByCode[code] ?? code,
      amount,
    };
    if (type === "revenue") incomeBreakdown.push(row);
    else expenseBreakdown.push(row);
  }
  incomeBreakdown.sort((a, b) => a.account_code.localeCompare(b.account_code));
  expenseBreakdown.sort((a, b) => a.account_code.localeCompare(b.account_code));

  const income_total = incomeBreakdown.reduce((s, r) => s + r.amount, 0);
  const expense_total = expenseBreakdown.reduce((s, r) => s + r.amount, 0);

  return {
    income_total,
    expense_total,
    net_income: income_total - expense_total,
    income_breakdown: incomeBreakdown,
    expense_breakdown: expenseBreakdown,
  };
}
