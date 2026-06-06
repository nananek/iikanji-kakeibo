// Phase E3-C-2: 試算表 (Trial Balance) のクライアントサイド集計。
//
// journals_client.fetchJournalsForYear が返した正規化 entry 配列から、
// 科目別の借方合計 / 貸方合計を計算する純粋関数。
//
// 設計書 §12.3 「レポート集計のクライアントサイド化」参照。
//
// サーバ側 app/services/reports.compute_trial_balance と並存する。Phase E7
// 一斉移行後にサーバ側を削除 (E3-F)。本 PR はサーバ側を残したまま JS 版を
// 追加し、UI 統合は別 PR (E3-C-2b) で行う。


/**
 * 試算表の借方/貸方合計を科目別に計算。
 *
 * @param {Array<Object>} entries
 *   journals_client.fetchJournalsForYear() の戻り値形式:
 *   [{id, fiscal_year, fiscal_month, is_closing, lines: [{account_code,
 *     debit, credit}]}]
 * @param {Object} [options]
 * @param {number} [options.fiscalPeriodFrom=0]  集計対象の最小 fiscal_month
 * @param {number} [options.fiscalPeriodTo=16]   集計対象の最大 fiscal_month
 * @param {boolean} [options.includeClosing=false]
 *   true なら closing 仕訳 (is_closing) も含める
 * @returns {Array<Object>}
 *   [{account_code, debit, credit}] (account_code でソート済)
 *
 *   balance (借方残 / 貸方残) は account の normal_balance に依存するため、
 *   本関数では計算せず呼出側に委ねる。
 */
export function computeTrialBalance(entries, options = {}) {
  const {
    fiscalPeriodFrom = 0,
    fiscalPeriodTo = 16,
    includeClosing = false,
  } = options;

  if (!Array.isArray(entries)) {
    throw new TypeError("entries must be an array");
  }

  // account_code → { debit, credit }
  const sums = new Map();

  for (const entry of entries) {
    // E3-F PR-D-6-3b: 平文 fiscal_period / source は API から撤去済。期間判定は
    // 保持列 fiscal_month、closing 判定は is_closing を使う (null は 0 扱い)。
    const fp = entry.fiscal_month ?? 0;
    if (fp < fiscalPeriodFrom || fp > fiscalPeriodTo) continue;
    // closing 仕訳の除外
    if (!includeClosing && entry.is_closing) continue;

    for (const line of entry.lines || []) {
      const code = line.account_code;
      if (code == null) continue;  // 復号失敗で account_code 不明な行はスキップ
      const cur = sums.get(code) ?? { debit: 0, credit: 0 };
      cur.debit += line.debit ?? 0;
      cur.credit += line.credit ?? 0;
      sums.set(code, cur);
    }
  }

  return [...sums.entries()]
    .map(([account_code, { debit, credit }]) => ({
      account_code,
      debit,
      credit,
    }))
    .sort((a, b) => a.account_code.localeCompare(b.account_code));
}


/**
 * 科目別 normal_balance に基づいて借方残/貸方残を計算する helper。
 *
 * @param {Object} row              {account_code, debit, credit}
 * @param {"debit"|"credit"} normalBalance
 * @returns {number}                残高 (正の値 = normal_balance 側、負 = 反対側)
 *
 * 例: 資産科目 (normal_balance="debit") で debit=1000 credit=300 → 700
 *     負債科目 (normal_balance="credit") で debit=200 credit=500 → 300
 */
export function balanceOf(row, normalBalance) {
  if (normalBalance === "debit") return row.debit - row.credit;
  if (normalBalance === "credit") return row.credit - row.debit;
  throw new Error(`balanceOf: unsupported normalBalance: ${normalBalance}`);
}


// B/S 科目 (資産/負債/純資産) は記帳開始以来の累計を繰り越す。P/L 科目
// (収益/費用) は年度ごとにリセットするため繰り越さない。
const BS_TYPES = new Set(["asset", "liability", "equity"]);

/**
 * 前年度以前の全 entries から B/S 科目の繰越残高 (opening) を算出する。
 *
 * v4 サーバ試算表の `opening = sum(date < 年初, include closing)` を E2EE
 * クライアント側で再現するためのもの。当年度の試算表で B/S 科目の残高が
 * 「記帳開始以来の累計」になるよう、前年度以前の借方-貸方を normal_balance
 * 符号で集計して opening に注入する。closing 仕訳も含める (純資産へ畳まれる
 * 損益振替を反映)。P/L 科目は対象外 (年度リセット)。
 *
 * @param {Array} priorEntries  前年度以前 (fiscal_year < 当年) の復号済み entries
 *   [{lines: [{account_code, debit, credit}]}]
 * @param {Object} accountsMeta  {code: {type, normal_balance, ...}}
 * @returns {Object}  {account_code: opening_balance}  (B/S 科目のみ、符号付き)
 */
export function computeBsOpeningFromPrior(priorEntries, accountsMeta) {
  const sums = new Map(); // code -> {debit, credit}
  for (const entry of priorEntries || []) {
    for (const line of entry.lines || []) {
      const code = line.account_code;
      if (code == null) continue;
      const meta = accountsMeta[code];
      if (!meta || !BS_TYPES.has(meta.type)) continue;
      const cur = sums.get(code) ?? { debit: 0, credit: 0 };
      cur.debit += line.debit ?? 0;
      cur.credit += line.credit ?? 0;
      sums.set(code, cur);
    }
  }
  const opening = {};
  for (const [code, row] of sums) {
    opening[code] = balanceOf(row, accountsMeta[code].normal_balance);
  }
  return opening;
}
