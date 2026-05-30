// 科目無効化時の残高振替の from/to 決定ロジック (純粋関数)。
//
// 残高がある科目を無効化する際、その残高を別の有効科目へ振り替える。
// 振替元 (from) / 振替先 (to) は科目の normal_balance と残高の符号で決まる。
// サーバ旧実装 (accounts.api_update の create_transfer_entry 呼び出し) と同じ:
//
//   balance > 0:
//     normalBalance=="debit"  → from=account, to=transfer
//     normalBalance=="credit" → from=transfer, to=account
//   balance < 0 (符号反転):
//     normalBalance=="debit"  → from=transfer, to=account
//     normalBalance=="credit" → from=account, to=transfer
//
// amount は常に正値 (絶対値) を返し buildTransferEntry の amount に渡す。
// buildTransferEntry は amount 正値で debit=toAccountCode / credit=fromAccountCode
// を組むため、ここで決めた from/to がそのまま借方・貸方に反映される。

/**
 * @param {Object} account       {code, normalBalance: "debit"|"credit"}
 * @param {Object} transferTo    {code}
 * @param {number} balance       _get_account_balance と同じ符号付き残高
 * @returns {{fromAccountCode: string, toAccountCode: string, amount: number}}
 */
export function computeDeactivateTransfer(account, transferTo, balance) {
  const positive = balance > 0;
  const isDebit = account.normalBalance === "debit";
  // balance>0 かつ debit、または balance<0 かつ credit → from=account
  const fromIsAccount = positive === isDebit;
  return {
    fromAccountCode: fromIsAccount ? account.code : transferTo.code,
    toAccountCode: fromIsAccount ? transferTo.code : account.code,
    amount: Math.abs(balance),
  };
}
