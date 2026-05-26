// Phase E3-F-3a: 試算表 view 構築の純粋関数。
//
// computeTrialBalance の結果 (account_code → debit/credit) と
// accountsMeta (code → {type, normal_balance, name}) を結合して、
// 科目区分 (asset/liability/equity/revenue/expense) ごとに行を
// 並べた view 用構造を返す。
//
// opening 残高はサーバ側 view が確定済 BCB から取得していた累計だが、
// 本 PR ではサーバ集計を撤去するため、当面 opening=0 とする
// (BCB 統合は後続 PR で対応)。
//
// 戻り値:
//   {
//     sections: [
//       {
//         typeCode: "asset"|"liability"|"equity"|"revenue"|"expense",
//         typeName: "資産"|"負債"|"純資産"|"収益"|"費用",
//         rows: [{code, name, opening, debit, credit, balance}],
//         subtotal: {opening, debit, credit, balance},
//       },
//       ...
//     ],
//   }


const TYPE_ORDER = ["asset", "liability", "equity", "revenue", "expense"];
const TYPE_NAMES = {
  asset: "資産",
  liability: "負債",
  equity: "純資産",
  revenue: "収益",
  expense: "費用",
};


/**
 * 試算表の view 構造を組み立てる純粋関数。
 *
 * @param {Array<{account_code: string, debit: number, credit: number}>} jsRows
 *   computeTrialBalance の戻り値
 * @param {Object<string, {type: string, normal_balance: string, name: string}>} accountsMeta
 *   科目コード → {type, normal_balance, name} のマップ
 * @returns {{sections: Array<Object>}}
 */
export function composeTrialBalanceView(jsRows, accountsMeta) {
  if (!Array.isArray(jsRows)) {
    throw new TypeError("jsRows must be an array");
  }
  if (!accountsMeta || typeof accountsMeta !== "object") {
    throw new TypeError("accountsMeta must be an object");
  }

  // 科目区分ごとにバケツ
  const buckets = {};
  for (const t of TYPE_ORDER) {
    buckets[t] = { rows: [], subtotal: { opening: 0, debit: 0, credit: 0, balance: 0 } };
  }

  for (const r of jsRows) {
    const meta = accountsMeta[r.account_code];
    if (!meta) continue;
    const typeCode = meta.type;
    if (!(typeCode in buckets)) continue;
    const isDebitNormal = meta.normal_balance === "debit";
    const opening = 0;  // BCB 統合後に拡張
    const balance = isDebitNormal
      ? opening + r.debit - r.credit
      : opening + r.credit - r.debit;
    const row = {
      code: r.account_code,
      name: meta.name || r.account_code,
      opening,
      debit: r.debit,
      credit: r.credit,
      balance,
    };
    buckets[typeCode].rows.push(row);
    buckets[typeCode].subtotal.opening += opening;
    buckets[typeCode].subtotal.debit += r.debit;
    buckets[typeCode].subtotal.credit += r.credit;
    buckets[typeCode].subtotal.balance += balance;
  }

  const sections = [];
  for (const t of TYPE_ORDER) {
    if (buckets[t].rows.length === 0) continue;
    buckets[t].rows.sort((a, b) => a.code.localeCompare(b.code));
    sections.push({
      typeCode: t,
      typeName: TYPE_NAMES[t],
      rows: buckets[t].rows,
      subtotal: buckets[t].subtotal,
    });
  }

  return { sections };
}
