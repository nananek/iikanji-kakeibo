// Phase E3-C-7: 総勘定元帳 (Ledger) のクライアントサイド集計。
//
// 指定 account_code の全 line を時系列順に並べ、各行で running balance を
// 計算する純粋関数。
//
// サーバ側 app/views/reports.ledger と並存。UI 統合は別 PR (E3-C-7b)、
// サーバ側削除は Phase E7 (E3-F)。
//
// 設計上の制約:
//   - date は暗号化されているため、サーバ側 ORDER BY date と同じ順序を
//     クライアントで再現できない。代わりに entry.id 順 (= 作成順、概ね
//     時系列と一致) でソートする。
//   - opening_balance (期首残高/前期繰越) は呼出側が指定する責務。
//     前年度の computeLedger 結果の最終 balance を渡すパターン。


/**
 * 指定科目の元帳行を計算。
 *
 * @param {Array<Object>} entries
 *   journals_client の戻り値形式 (1 年分の正規化 entry 配列)
 * @param {Object} options
 * @param {string} options.accountCode      対象勘定科目コード
 * @param {"debit"|"credit"} options.normalBalance
 *                                          科目の normal_balance
 * @param {number} [options.openingBalance=0]
 *                                          期首残高 (前期繰越)
 * @param {number} [options.fiscalPeriodFrom=0]
 * @param {number} [options.fiscalPeriodTo=16]
 * @param {boolean} [options.includeClosing=true]
 *                                          元帳は通常 closing 仕訳も含む
 *                                          (損益振替の挙動を確認したいケース)
 *
 * @returns {Object} {
 *   opening_balance,
 *   rows: [{entry_id, fiscal_period, date, description, debit, credit,
 *           balance, counterparts}],
 *   closing_balance,    // 最終 balance
 *   total_debit,        // 期間内借方合計
 *   total_credit,       // 期間内貸方合計
 * }
 *
 * counterparts: 当該 entry 内の他 line の account_code をカンマ区切り。
 *   復号失敗 (account_code === null) の line も含む可能性あり。
 */
export function computeLedger(entries, options) {
  if (!Array.isArray(entries)) {
    throw new TypeError("entries must be an array");
  }
  if (!options || !options.accountCode) {
    throw new TypeError("options.accountCode is required");
  }
  const {
    accountCode,
    normalBalance,
    openingBalance = 0,
    fiscalPeriodFrom = 0,
    fiscalPeriodTo = 16,
    includeClosing = true,
  } = options;
  if (normalBalance !== "debit" && normalBalance !== "credit") {
    throw new TypeError(
      "options.normalBalance must be 'debit' or 'credit'",
    );
  }

  // entry.id 昇順 (作成順 = 概ね時系列)
  const sorted = [...entries].sort((a, b) => {
    const ai = a.id ?? 0;
    const bi = b.id ?? 0;
    return ai - bi;
  });

  const rows = [];
  let balance = openingBalance;
  let total_debit = 0;
  let total_credit = 0;

  for (const entry of sorted) {
    // E3-F PR-D-6-3b: 平文 fiscal_period / source は API から撤去済。期間判定は
    // 保持列 fiscal_month、closing 判定は is_closing を使う。
    const fp = entry.fiscal_month ?? 0;
    if (fp < fiscalPeriodFrom || fp > fiscalPeriodTo) continue;
    if (!includeClosing && entry.is_closing) continue;

    // 当該科目の line のみ抽出 (1 entry 内に同科目の複数 line もあり得る)
    let entryDebit = 0;
    let entryCredit = 0;
    let hasMatch = false;
    const counterpartSet = new Set();
    for (const line of entry.lines || []) {
      if (line.account_code === accountCode) {
        entryDebit += line.debit ?? 0;
        entryCredit += line.credit ?? 0;
        hasMatch = true;
      } else if (line.account_code != null) {
        counterpartSet.add(line.account_code);
      }
    }
    if (!hasMatch) continue;

    // balance 更新
    if (normalBalance === "debit") {
      balance += entryDebit - entryCredit;
    } else {
      balance += entryCredit - entryDebit;
    }
    total_debit += entryDebit;
    total_credit += entryCredit;

    rows.push({
      entry_id: entry.id,
      fiscal_period: fp,
      date: entry.date ?? null,
      description: entry.description ?? "",
      debit: entryDebit,
      credit: entryCredit,
      balance,
      counterparts: [...counterpartSet].sort().join(", "),
    });
  }

  return {
    opening_balance: openingBalance,
    rows,
    closing_balance: balance,
    total_debit,
    total_credit,
  };
}
